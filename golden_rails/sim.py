"""Mesoscopic timetable planning and stochastic operations simulation.

Each segment is split into blocks; each block has one channel per track.
A channel keeps a sorted list of occupations (entry, exit, direction).
Feasibility of a new occupation (entry t, running time r, direction d):
  * same direction: no overtaking inside the block, minimum headway h at
    entry and at exit with respect to every train before/after it;
  * opposite direction (only on bidirectional channels, i.e. single track or
    double track in wrong-line working during an incident): the occupations
    must not overlap (plus buffer c);
  * blocked intervals (incidents): no overlap at all.
"""
from __future__ import annotations

import heapq
import math
import random
import zlib
from bisect import bisect_left, bisect_right, insort
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .network import DATA, Network, channel_layout

HEADWAY = 3.0          # min, same-direction headway (operations)
PLAN_BUFFER = 1.0      # min, buffer time added to headway in timetable planning
OPP_BUFFER = 1.0       # min, opposing-train buffer on single track
LOOKBACK = 90.0        # min, max block occupation considered
REROUTE_WAIT = 20.0    # min of expected wait before trying a detour
DWELL = {"FV": 2.0, "NV": 1.0, "GV": 0.0}
SUPPLEMENT = {"FV": 0.05, "NV": 0.05, "GV": 0.05}   # recovery margin (Regelzuschlag)
PRIORITY = {"FV": 0, "NV": 1, "GV": 2}
DAY_FACTOR = 330.0     # weekday-equivalent operating days per year


def crc(*parts) -> int:
    return zlib.crc32(":".join(map(str, parts)).encode())


def hm(s: str) -> float:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


# ----------------------------------------------------------------------------
# Disruption parameters ("standard disruptions")
# ----------------------------------------------------------------------------
@dataclass
class DisruptionModel:
    # primary (exogenous) delays at train origin: probability, mean (exp. dist.)
    p_origin: dict = field(default_factory=lambda: {"FV": 0.30, "NV": 0.25, "GV": 0.50})
    mean_origin: dict = field(default_factory=lambda: {"FV": 4.5, "NV": 5.5, "GV": 25.0})
    # dwell-time extensions at each scheduled stop
    p_dwell: float = 0.20
    mean_dwell: float = 1.0
    # running-time variation (multiplicative, normal, sd)
    run_sd: float = 0.03
    # infrastructure incidents, per km per day
    rate_track_fault: float = 1.0e-3    # one track blocked (signal, switch, track defect) per track-km
    rate_full_closure: float = 1.5e-4   # all tracks blocked (person/police/fire/tree) per route-km
    median_track_fault: float = 45.0    # min, lognormal
    median_full_closure: float = 75.0
    sigma_duration: float = 0.8
    scale: float = 1.0                  # global multiplier on exogenous train delays

    def to_dict(self):
        return self.__dict__.copy()


# ----------------------------------------------------------------------------
class Channel:
    __slots__ = ("dir", "entries", "exits", "dirs", "blocked", "bidir")

    def __init__(self, d):
        self.dir = d
        self.entries: list[float] = []
        self.exits: list[float] = []
        self.dirs: list[int] = []
        self.blocked: list[tuple[float, float]] = []
        self.bidir: list[tuple[float, float]] = []

    def usable(self, d: int, t: float) -> bool:
        if self.dir == 0 or self.dir == d:
            return True
        for a, b in self.bidir:
            if a <= t < b:
                return True
        return False

    def fit(self, t: float, r: float, d: int, h: float = HEADWAY) -> float:
        for _ in range(10000):
            moved = False
            for b0, b1 in self.blocked:
                if t < b1 and t + r > b0:
                    t = b1
                    moved = True
            entries = self.entries
            lo = bisect_left(entries, t - LOOKBACK)
            hi = bisect_right(entries, t + r + h)
            for i in range(lo, hi):
                e = entries[i]
                x = self.exits[i]
                if self.dirs[i] == d:
                    if e <= t:
                        nt = max(e + h, x + h - r)
                        if nt > t + 1e-9:
                            t = nt
                            moved = True
                            break
                    elif t + h > e or t + r + h > x:
                        t = max(e + h, x + h - r)
                        moved = True
                        break
                else:
                    if t < x + OPP_BUFFER and t + r + OPP_BUFFER > e:
                        t = x + OPP_BUFFER
                        moved = True
                        break
            if not moved:
                return t
        raise RuntimeError("fit did not converge")

    def book(self, t: float, r: float, d: int):
        i = bisect_right(self.entries, t)
        self.entries.insert(i, t)
        self.exits.insert(i, t + r)
        self.dirs.insert(i, d)


class Infra:
    """Block/channel state for one simulation run."""

    def __init__(self, net: Network, headway: float = HEADWAY):
        self.net = net
        self.h = headway
        self.blocks: dict[tuple[str, int], list[Channel]] = {}
        for s in net.segments.values():
            for bi in range(s.n_blocks()):
                self.blocks[(s.id, bi)] = [Channel(d) for d in channel_layout(s.tracks)]
        self.full_closures: dict[str, list[tuple[float, float]]] = {}

    def earliest(self, key, t, r, d, prefer_slow: bool, pref: int | None = None):
        chans = self.blocks[key]
        if pref is not None and pref < len(chans):
            ch = chans[pref]
            if ch.usable(d, t):
                ft = ch.fit(t, r, d, self.h)
                if ft <= t + 1e-9:
                    return ft, ch
        order = range(len(chans) - 1, -1, -1) if prefer_slow else range(len(chans))
        best_t, best_c = math.inf, None
        for ci in order:
            ch = chans[ci]
            if not ch.usable(d, t):
                continue
            ft = ch.fit(t, r, d, self.h)
            if ft < best_t - 1e-9:
                best_t, best_c = ft, ch
        if best_c is None:  # every channel is the wrong direction & no bidir window
            for ch in chans:
                ft = ch.fit(t, r, d, self.h)
                if ft < best_t:
                    best_t, best_c = ft, ch
        return best_t, best_c

    # -- incidents --------------------------------------------------------
    def add_track_fault(self, sid, bi, track_idx, t0, t1):
        chans = self.blocks[(sid, bi)]
        ch = chans[track_idx % len(chans)]
        ch.blocked.append((t0, t1))
        d = ch.dir
        if d != 0 and not any(c.dir in (d, 0) for c in chans if c is not ch):
            for c in chans:
                if c is not ch:
                    c.bidir.append((t0, t1))

    def add_full_closure(self, sid, bi, t0, t1):
        for ch in self.blocks[(sid, bi)]:
            ch.blocked.append((t0, t1))
        self.full_closures.setdefault(sid, []).append((t0, t1))

    def closure_penalty(self, t):
        def f(sid):
            pen = 0.0
            for a, b in self.full_closures.get(sid, ()):
                if a <= t < b:
                    pen = max(pen, b - t)
            return pen
        return f


# ----------------------------------------------------------------------------
@dataclass
class Train:
    id: str
    ttype: str
    vmax: float
    desired_dep: float
    stops: list[str]                       # scheduled stop nodes (incl. origin & dest)
    steps: list = field(default_factory=list)  # [(seg_id, block_idx, dir, run, end_node, is_stop)]
    plan_entry: list = field(default_factory=list)
    plan_arr: list = field(default_factory=list)
    plan_ch: list = field(default_factory=list)


def expand_path(net: Network, path, ttype, vmax, stop_set):
    steps = []
    for sid, d in path:
        seg = net.segments[sid]
        nb = seg.n_blocks()
        r = net.run_minutes(seg, ttype, vmax) / nb
        end_node = seg.b if d == 1 else seg.a
        idxs = range(nb) if d == 1 else range(nb - 1, -1, -1)
        for k, bi in enumerate(idxs):
            last = k == nb - 1
            steps.append([sid, bi, d, r, end_node if last else None,
                          last and end_node in stop_set])
    return steps


def route_train(net: Network, tr: Train, via: list[str]):
    stop_set = set(tr.stops)
    path = []
    for a, b in zip(via[:-1], via[1:]):
        path += net.shortest_path(a, b, tr.ttype, tr.vmax)
    tr.steps = expand_path(net, path, tr.ttype, tr.vmax, stop_set)
    if tr.steps:
        tr.steps[-1][5] = True


def build_trains(net: Network, data_dir=DATA) -> list[Train]:
    trains: list[Train] = []
    fv = pd.read_csv(data_dir / "fv_lines.csv")
    for r in fv.itertuples():
        stops = r.stops.split()
        off = crc(r.id) % 30
        t0, t1 = hm(r.first_dep) + off, hm(r.last_dep) + off
        for direction, seq in (("a", stops), ("b", stops[::-1])):
            k = 0
            t = t0 + (0 if direction == "a" else 7)
            while t <= t1 + 7:
                tr = Train(f"{r.id}-{direction}-{k}", "FV", float(r.vmax), t, seq)
                route_train(net, tr, seq)
                trains.append(tr)
                t += r.headway_min
                k += 1
    gv = pd.read_csv(data_dir / "freight_flows.csv")
    for r in gv.itertuples():
        via = r.via.split()
        n = int(r.trains_per_day_per_dir)
        for direction, seq in (("a", via), ("b", via[::-1])):
            gap = 1440.0 / n
            off = (crc(r.id, direction) % 1000) / 1000 * gap
            for k in range(n):
                tr = Train(f"{r.id}-{direction}-{k}", "GV", 100.0, off + k * gap,
                           [seq[0], seq[-1]])
                route_train(net, tr, seq)
                trains.append(tr)
    for s in net.segments.values():
        if s.nv_tph <= 0 or "NV" not in s.types:
            continue
        hw = 60.0 / s.nv_tph
        for direction, (a, b) in (("a", (s.a, s.b)), ("b", (s.b, s.a))):
            off = (crc(s.id, direction) % 1000) / 1000 * hw
            t, k = 300.0 + off, 0
            while t < 1440:
                tr = Train(f"NV-{s.id}-{direction}-{k}", "NV", 160.0, t, [a, b])
                d = 1 if a == s.a else -1
                tr.steps = expand_path(net, [(s.id, d)], "NV", 160.0, {b})
                trains.append(tr)
                t += hw
                k += 1
    return trains


# ----------------------------------------------------------------------------
def _step_keys(tr: Train):
    return [(st[0], st[1], st[2]) for st in tr.steps]


def plan_timetable(net: Network, trains: list[Train], base: dict | None = None) -> dict:
    """Conflict-free timetable.

    Without ``base``: every train is planned, by priority (FV, NV, GV) then time.
    With ``base`` (train id -> planned Train of the current timetable): trains whose
    block sequence is unchanged keep their current slots exactly; only trains with a
    changed route (e.g. moved onto a new line) are re-planned into the gaps.
    """
    infra = Infra(net, HEADWAY + PLAN_BUFFER)
    to_plan = []
    n_fixed = 0
    for tr in trains:
        b = base.get(tr.id) if base else None
        if b is not None and _step_keys(b) == _step_keys(tr):
            tr.plan_entry, tr.plan_arr = list(b.plan_entry), list(b.plan_arr)
            tr.plan_ch = []
            for (sid, bi, d, *_), te, ta, ci in zip(tr.steps, b.plan_entry, b.plan_arr, b.plan_ch):
                chans = infra.blocks[(sid, bi)]
                if ci >= len(chans) or not chans[ci].usable(d, te):
                    ci = next(k for k, c in enumerate(chans) if c.usable(d, te))
                chans[ci].book(te, ta - te, d)
                tr.plan_ch.append(ci)
            n_fixed += 1
        else:
            to_plan.append(tr)
    order = sorted(to_plan, key=lambda tr: (PRIORITY[tr.ttype], tr.desired_dep))
    planned_wait = 0.0
    for tr in order:
        supp = 1.0 + SUPPLEMENT[tr.ttype]
        t = tr.desired_dep
        tr.plan_entry, tr.plan_arr, tr.plan_ch = [], [], []
        slow = tr.ttype != "FV"
        for sid, bi, d, r, end_node, is_stop in tr.steps:
            rp = r * supp
            te, ch = infra.earliest((sid, bi), t, rp, d, slow)
            planned_wait += te - t
            ch.book(te, rp, d)
            tr.plan_entry.append(te)
            tr.plan_ch.append(infra.blocks[(sid, bi)].index(ch))
            t = te + rp
            tr.plan_arr.append(t)
            if is_stop:
                t += DWELL[tr.ttype]
    return {"planned_wait_min": planned_wait, "n_fixed": n_fixed, "n_replanned": len(to_plan)}


# ----------------------------------------------------------------------------
def sample_incidents(net: Network, dm: DisruptionModel, seed: int):
    inc = []
    for s in net.segments.values():
        nb = s.n_blocks()
        rng = random.Random(crc(seed, "full", s.id))
        n = _poisson(rng, dm.rate_full_closure * s.length)
        for _ in range(n):
            pos = rng.random()
            t0 = 240 + rng.random() * 1200
            dur = dm.median_full_closure * math.exp(rng.gauss(0, dm.sigma_duration))
            inc.append(("full", s.id, min(nb - 1, int(pos * nb)), None, t0, t0 + dur))
        for k in range(s.tracks):
            rng = random.Random(crc(seed, "track", s.id, k))
            n = _poisson(rng, dm.rate_track_fault * s.length)
            for _ in range(n):
                pos = rng.random()
                t0 = 240 + rng.random() * 1200
                dur = dm.median_track_fault * math.exp(rng.gauss(0, dm.sigma_duration))
                inc.append(("track", s.id, min(nb - 1, int(pos * nb)), k, t0, t0 + dur))
    return inc


def _poisson(rng: random.Random, lam: float) -> int:
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def simulate_day(net: Network, trains: list[Train], dm: DisruptionModel, seed: int,
                 reroute: bool = True) -> dict:
    infra = Infra(net)
    incidents = sample_incidents(net, dm, seed)
    for kind, sid, bi, k, t0, t1 in incidents:
        if kind == "full":
            infra.add_full_closure(sid, bi, t0, t1)
        else:
            infra.add_track_fault(sid, bi, k, t0, t1)

    # per-train random state (common random numbers across scenarios)
    pq = []
    state = {}
    for idx, tr in enumerate(trains):
        rng = random.Random(crc(seed, tr.id))
        prim = 0.0
        if rng.random() < dm.p_origin[tr.ttype]:
            prim = rng.expovariate(1.0 / (dm.mean_origin[tr.ttype] * dm.scale))
        steps = [list(s) for s in tr.steps]
        plan_entry = list(tr.plan_entry)
        plan_arr = list(tr.plan_arr)
        plan_ch = list(tr.plan_ch)
        state[idx] = (rng, steps, plan_entry, plan_arr, plan_ch)
        heapq.heappush(pq, (plan_entry[0] + prim, idx, 0))

    delay_by_type = {"FV": 0.0, "NV": 0.0, "GV": 0.0}
    stops_by_type = {"FV": 0, "NV": 0, "GV": 0}
    punctual_by_type = {"FV": 0, "NV": 0, "GV": 0}
    final_delay = {"FV": 0.0, "NV": 0.0, "GV": 0.0}
    seg_delay: dict[str, float] = {}
    n_reroutes = 0

    while pq:
        t, idx, i = heapq.heappop(pq)
        tr = trains[idx]
        rng, steps, plan_entry, plan_arr, plan_ch = state[idx]
        sid, bi, d, r, end_node, is_stop = steps[i]
        ready = max(t, plan_entry[i])
        ract = r * max(0.97, 1.0 + rng.gauss(0, dm.run_sd))
        te, ch = infra.earliest((sid, bi), ready, ract, d, tr.ttype != "FV", plan_ch[i])

        if reroute and tr.ttype != "NV" and te - ready > REROUTE_WAIT and sid in infra.full_closures:
            new = _try_reroute(net, infra, tr, steps, i, ready, te, plan_entry, plan_arr, plan_ch)
            if new is not None:
                steps, plan_entry, plan_arr, plan_ch = new
                state[idx] = (rng, steps, plan_entry, plan_arr, plan_ch)
                n_reroutes += 1
                heapq.heappush(pq, (ready, idx, i))
                continue

        ch.book(te, ract, d)
        wait = te - ready
        if wait > 0:
            seg_delay[sid] = seg_delay.get(sid, 0.0) + wait
        tx = te + ract
        nxt = tx
        if is_stop:
            dl = max(0.0, tx - plan_arr[i])
            last = i == len(steps) - 1
            if tr.ttype == "FV" or last:
                delay_by_type[tr.ttype] += dl
                stops_by_type[tr.ttype] += 1
                if dl < 6.0:
                    punctual_by_type[tr.ttype] += 1
            if last:
                final_delay[tr.ttype] += dl
            nxt = tx + DWELL[tr.ttype]
            if rng.random() < dm.p_dwell and tr.ttype != "GV":
                nxt += rng.expovariate(1.0 / (dm.mean_dwell * dm.scale))
        if i + 1 < len(steps):
            heapq.heappush(pq, (nxt, idx, i + 1))

    total = sum(delay_by_type.values())
    return {
        "seed": seed,
        "delay_min": total,
        **{f"delay_{k}": v for k, v in delay_by_type.items()},
        **{f"final_delay_{k}": v for k, v in final_delay.items()},
        **{f"punct_{k}": punctual_by_type[k] / max(1, stops_by_type[k]) for k in stops_by_type},
        "n_incidents": len(incidents),
        "n_reroutes": n_reroutes,
        "seg_wait": seg_delay,
    }


def _try_reroute(net, infra, tr, steps, i, ready, te, plan_entry, plan_arr, plan_ch):
    """Detour from the current node to the next scheduled stop around a closure."""
    sid, bi, d, *_ = steps[i]
    seg = net.segments[sid]
    here = seg.a if d == 1 else seg.b
    j = i
    while j < len(steps) and not steps[j][5]:
        j += 1
    if j >= len(steps):
        return None
    target = steps[j][4]
    pen = infra.closure_penalty(ready)
    cur_time = (te - ready) + sum(s[3] for s in steps[i:j + 1])
    try:
        path = net.shortest_path(here, target, tr.ttype, tr.vmax, extra_cost=pen)
    except ValueError:
        return None
    if not path or any(s == sid for s, _ in path):
        return None
    alt_time = sum(net.run_minutes(net.segments[s], tr.ttype, tr.vmax) + 1.0 + pen(s)
                   for s, _ in path)
    if alt_time + 5.0 >= cur_time:
        return None
    new_steps = expand_path(net, path, tr.ttype, tr.vmax, {target})
    new_steps[-1][5] = True
    n = len(new_steps)
    steps_out = steps[:i] + new_steps + steps[j + 1:]
    pe = plan_entry[:i] + [-math.inf] * n + plan_entry[j + 1:]
    pa = plan_arr[:i] + [math.inf] * (n - 1) + [plan_arr[j]] + plan_arr[j + 1:]
    pc = plan_ch[:i] + [None] * n + plan_ch[j + 1:]
    return steps_out, pe, pa, pc
