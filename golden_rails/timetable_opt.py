"""Timetable softening: small shifts of whole trains that make the timetable more robust.

Rules (the "soft" timetable keeps throughput and arrival rates at every node):
  * each passenger train may move by at most ``MAX_SHIFT['P']`` minutes, freight by ``MAX_SHIFT['GV']``;
    stops, stopping pattern and running times stay the same;
  * every node arrival / departure stays in the same clock hour, so the number of trains per node,
    hour and category is exactly unchanged;
  * vehicle circulations stay feasible (minimum turnaround); pieces of the same trip move together.

Objective (surrogate, evaluated on the planned occupations of the current infrastructure): for each
pair of consecutive occupations of a track channel, platform track or throat route group, the buffer
below a target is penalised quadratically and weighted with the passengers of the following train;
short turnaround buffers are penalised the same way. A local search (first-improvement, several
passes) picks the shifts. The shifted timetable is then planned conflict-free and evaluated with the
full Monte-Carlo simulation like any other measure.
"""
from __future__ import annotations

import math
import random
from bisect import bisect_left, insort
from collections import defaultdict

from . import fleet
from .sim import OPP_BUFFER, SUPPLEMENT, Infra

MAX_SHIFT = {"P": 3, "GV": 15}
TARGET = {"block": 2.0, "node": 1.5, "turn": 5.0}
WEIGHT = {"FV": 350.0, "NV": 140.0, "S": 90.0, "GV": 30.0}


class _Chan:
    __slots__ = ("keys", "kind")

    def __init__(self, kind):
        self.keys: list = []     # sorted (entry, oid)
        self.kind = kind         # "block" or "node"


def _occupations(net, trains):
    """Planned occupations per train: (channel key, entry, exit, dir, follower headway)."""
    infra = Infra(net)        # no planning buffer: real headways
    occ = defaultdict(list)
    for i, tr in enumerate(trains):
        supp = 1.0 + SUPPLEMENT[tr.ttype]
        for (sid, bi, d, r, *_), te, ci in zip(tr.steps, tr.plan_entry, tr.plan_ch):
            occ[i].append(((sid, bi, ci), te, te + r * supp, d, infra.h(sid, tr.equipped)))
        for key, ci, t0, r in tr.plan_nodes or ():
            occ[i].append(((key[0], 0, ci), t0, t0 + r, 0, 0.0))
    bg = []
    for key, chans in infra.blocks.items():
        if key[0][0] == "@":
            for ci, ch in enumerate(chans):
                for e, x in zip(ch.entries, ch.exits):
                    bg.append(((key[0], 0, ci), e, x, 0, 0.0))
    return occ, bg


def _node_times(tr):
    out = [tr.plan_dep0] if tr.plan_dep0 > -math.inf else []
    for st, ta in zip(tr.steps, tr.plan_arr):
        if st[4] is not None and ta < math.inf:
            out.append(ta)
    return out


def optimise(net, trains, passes: int = 3, seed: int = 1, max_shift: dict | None = None, log=print) -> dict:
    """Return {train id: shift in minutes} for planned ``trains`` (plan_timetable already run)."""
    max_shift = max_shift or MAX_SHIFT
    occ, bg = _occupations(net, trains)
    chans: dict = {}
    info: dict = {}          # oid -> [chan key, entry, exit, dir, h, weight, train idx]
    oid = 0
    for k, e, x, d, h in bg:
        c = chans.setdefault(k, _Chan("node"))
        info[oid] = [k, e, x, d, h, 0.0, -1]
        insort(c.keys, (e, oid))
        oid += 1
    own = defaultdict(list)
    for i, lst in occ.items():
        w = WEIGHT[trains[i].ttype]
        for k, e, x, d, h in lst:
            c = chans.setdefault(k, _Chan("node" if k[0][0] == "@" else "block"))
            info[oid] = [k, e, x, d, h, w, i]
            insort(c.keys, (e, oid))
            own[i].append(oid)
            oid += 1

    def pair_pen(c, a, b):
        ia, ib = info[a], info[b]
        if c.kind == "node":
            gap = ib[1] - ia[2]
            tgt = TARGET["node"]
        elif ia[3] == ib[3] and ia[3] != 0:
            gap = min(ib[1] - ia[1], ib[2] - ia[2]) - ib[4]
            tgt = TARGET["block"]
        else:
            gap = ib[1] - ia[2] - OPP_BUFFER
            tgt = TARGET["block"]
        return ib[5] * max(0.0, tgt - gap) ** 2

    def local(c, pos):
        """Penalty of the pairs touching position pos (prev, pos) and (pos, next)."""
        ks = c.keys
        p = 0.0
        if pos > 0:
            p += pair_pen(c, ks[pos - 1][1], ks[pos][1])
        if pos + 1 < len(ks):
            p += pair_pen(c, ks[pos][1], ks[pos + 1][1])
        return p

    def bridge(c, pos):
        ks = c.keys
        if 0 < pos < len(ks) - 1:
            return pair_pen(c, ks[pos - 1][1], ks[pos + 1][1])
        return 0.0

    def move_occ(o, s):
        """Shift occupation o by s minutes; returns the change of the channel penalty."""
        k, e, x = info[o][0], info[o][1], info[o][2]
        c = chans[k]
        pos = bisect_left(c.keys, (e, o))
        delta = -local(c, pos)
        delta += bridge(c, pos)
        del c.keys[pos]
        info[o][1], info[o][2] = e + s, x + s
        insort(c.keys, (e + s, o))
        pos = bisect_left(c.keys, (e + s, o))
        delta -= bridge(c, pos)
        delta += local(c, pos)
        return delta

    # units: pieces of the same trip move together
    unit_of = {}
    units = []
    for i, tr in enumerate(trains):
        if tr.pred is not None and tr.pred_kind == "cont" and tr.pred in unit_of:
            u = unit_of[tr.pred]
            units[u].append(i)
            unit_of[i] = u
        else:
            unit_of[i] = len(units)
            units.append([i])
    succ = {tr.pred: i for i, tr in enumerate(trains) if tr.pred is not None and tr.pred_kind == "turn"}
    shift = [0] * len(trains)

    def turn_pen(s_idx):
        tr = trains[s_idx]
        p = trains[tr.pred]
        if not tr.plan_entry or not p.plan_arr:
            return 0.0
        dep = (tr.plan_dep0 if tr.plan_dep0 > -math.inf else tr.plan_entry[0]) + shift[s_idx]
        buf = dep - (p.plan_arr[-1] + shift[tr.pred]) - fleet.MIN_TURN[tr.ttype]
        pen = WEIGHT[tr.ttype] * max(0.0, TARGET["turn"] - buf) ** 2
        return pen * 10.0 if buf < 0 else pen      # below the minimum turnaround: strongly penalised

    def turn_terms(u):
        t = 0.0
        for i in units[u]:
            if trains[i].pred is not None and trains[i].pred_kind == "turn":
                t += turn_pen(i)
            if i in succ:
                t += turn_pen(succ[i])
        return t

    def allowed(u, s):
        for i in units[u]:
            tr = trains[i]
            m = max_shift["GV"] if tr.ttype == "GV" else max_shift["P"]
            if abs(s) > m:
                return False
            for t in _node_times(tr):
                if math.floor((t + shift[i] + s) / 60.0) != math.floor(t / 60.0):
                    return False
        return True

    def apply(u, s):
        d = 0.0
        for i in units[u]:
            for o in own[i]:
                d += move_occ(o, s)
            shift[i] += s
        return d

    total0 = sum(sum(pair_pen(c, c.keys[j][1], c.keys[j + 1][1]) for j in range(len(c.keys) - 1))
                 for c in chans.values())
    rng = random.Random(seed)
    order = list(range(len(units)))
    total = total0
    for p in range(passes):
        rng.shuffle(order)
        improved = 0
        for u in order:
            base_turn = turn_terms(u)
            cur = shift[units[u][0]]
            best_d, best_s = -1e-6, 0
            m = max_shift["GV"] if trains[units[u][0]].ttype == "GV" else max_shift["P"]
            for target in range(-m, m + 1):
                s = target - cur
                if s == 0 or not allowed(u, s):
                    continue
                d = apply(u, s)
                d += turn_terms(u) - base_turn
                apply(u, -s)
                if d < best_d:
                    best_d, best_s = d, s
            if best_s:
                apply(u, best_s)
                total += best_d
                improved += 1
        log(f"timetable softening pass {p + 1}: {improved} trains moved, penalty {total0:,.0f} -> {total:,.0f}")
    return {trains[i].id: float(s) for i, s in enumerate(shift) if s}


def check_hour_counts(net, base_trains, new_trains) -> int:
    """Number of (node, hour, category) cells whose planned arrival count differs (should be 0 before
    re-planning; re-planning may move a few trains by conflicts)."""
    def counts(trains):
        c = defaultdict(int)
        for tr in trains:
            for t in _node_times(tr):
                c[(tr.ttype, math.floor(t / 60.0))] += 1
        return c
    a, b = counts(base_trains), counts(new_trains)
    return sum(1 for k in set(a) | set(b) if a.get(k, 0) != b.get(k, 0))
