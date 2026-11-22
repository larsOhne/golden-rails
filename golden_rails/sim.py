"""Mesoscopic corridor and node model: timetable planning and stochastic operations.

Corridors (segments) are split into blocks; a block has one channel per track
(corridor width = number of tracks, see ``network.channel_layout``).
A channel keeps a sorted list of occupations (entry, exit, direction, headway).
Feasibility of a new occupation (entry t, running time r, direction d):
  * same direction: no overtaking inside the block, minimum headway at entry and exit. The headway
    belongs to the *following* train: 2 min if the block has ETCS and the follower is ETCS-equipped,
    otherwise 3 min (mixed traffic on lineside signals);
  * opposite direction (only on bidirectional channels: single track, an odd
    extra track, or wrong-line working during an incident): occupations must
    not overlap (plus buffer);
  * blocked intervals (incidents, broken-down trains): no overlap at all.

Major nodes (``data/nodes_capacity.csv``) have platform tracks and a throat with a number of route
groups. Every passenger movement into or out of the node occupies a route group for ``TAU`` minutes;
a stopping train occupies a platform track from arrival to departure. Trains that touch only this
node (``data/node_background.csv``) occupy platforms and throat as background traffic.

Vehicles run in circulations (``fleet.build_circulations``): a late arriving vehicle delays its next
trip unless the turnaround buffer absorbs it or a hot reserve set at that node takes over. Vehicles
fail before departure (trip cancelled, or replaced by a reserve) or en route (the train blocks its
track, the rest of the trip is cancelled). Passengers of a cancelled trip wait for the next train of
the same line.

Conflicts are resolved by a standard priority rule
(ICE/IC > RE/RB > S-Bahn/tram-train > cargo), both when the timetable is
built and in operations, where a dispatcher looks ahead LOOKAHEAD minutes
and keeps a block free for an approaching higher-priority train.
"""
from __future__ import annotations

import heapq
import math
import os
import random
import zlib
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import fleet
from .network import DATA, Network, channel_layout

PLAN_BUFFER = 1.0      # min, buffer time added to headway in timetable planning
PLAN_NODE_BUFFER = 0.5  # min, buffer on platform/throat occupations in planning
OPP_BUFFER = 1.0       # min, opposing-train buffer on bidirectional track
LOOKBACK = 90.0        # min, max block occupation considered
LOOKAHEAD = 15.0       # min, dispatcher horizon for priority decisions
REROUTE_WAIT = 20.0    # min of expected wait before trying a detour
TYPES = ("FV", "NV", "S", "GV")
PAX_TYPES = ("FV", "NV", "S")
DWELL = {"FV": 2.0, "NV": 1.0, "S": 0.5, "GV": 0.0}
SUPPLEMENT = {"FV": 0.05, "NV": 0.05, "S": 0.05, "GV": 0.05}   # recovery margin (Regelzuschlag)
PRIORITY = {"FV": 0, "NV": 1, "S": 2, "GV": 3}   # standard priority rule, lower = higher priority
DAY_FACTOR = 330.0     # weekday-equivalent operating days per year

# node model
TAU = float(os.environ.get("GR_TAU", 1.5))   # min, route occupation per movement in a station throat
TAU_ETCS_FACTOR = 0.7                        # ETCS in the node shortens route occupation (equipped trains)
PLATFORM_PREP = {"FV": 12.0, "NV": 6.0, "S": 4.0}   # platform occupied before an originating departure
PLATFORM_TURN = {"FV": 12.0, "NV": 6.0, "S": 4.0}   # platform occupied after a terminating arrival
CANCEL_CAP = 150.0     # min, cap on the passenger delay of a cancelled train
LATE_SWAP = {"FV": 10.0, "NV": 5.0, "S": 5.0}    # reserve takes over if the inbound vehicle is this late
SWAP_DELAY_LATE = 3.0                            # min, departure delay when a reserve takes over
SWAP_DELAY_FAIL = {"FV": 15.0, "NV": 10.0, "S": 8.0}   # min, after a failure before departure

# Expected passengers on board (average over the run) per train
PAX_DEFAULT = {"FV": 300.0, "NV": 140.0, "S": 90.0}
FREIGHT_PAX_EQUIV = 0.0   # passenger-equivalent weight of a freight train (0 = passengers only)


def hour_factor(t_min: float) -> float:
    """Demand profile over the day (1.0 = average daytime off-peak)."""
    h = (t_min / 60.0) % 24
    if 6 <= h < 9 or 16 <= h < 19:
        return 1.4
    if 9 <= h < 16:
        return 1.0
    if 19 <= h < 22:
        return 0.8
    return 0.4


def crc(*parts) -> int:
    return zlib.crc32(":".join(map(str, parts)).encode())


def hm(s: str) -> float:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


# ----------------------------------------------------------------------------
# Disruption parameters ("standard disruptions")
# ----------------------------------------------------------------------------
OBSERVED = DATA / os.environ.get("GR_OBS", "observed_cal")
if not OBSERVED.exists():
    OBSERVED = DATA / "observed"


def _empirical_origin() -> dict:
    """Observed delays (minutes >= 1) when trains enter the model, per category (data/observed/)."""
    f = OBSERVED / "entry_delay_hist.csv"
    if not f.exists():
        f = OBSERVED / "origin_delay_hist.csv"
    if not f.exists():
        return {}
    h = pd.read_csv(f)
    h = h[h.d >= 1]
    return {c: (g.d.astype(float).tolist(), g.n.astype(float).tolist()) for c, g in h.groupby("cat")}


def _observed_p_origin(default: dict) -> dict:
    f = OBSERVED / "entry_delay_hist.csv"
    if f.exists():
        h = pd.read_csv(f)
        o = {c: float(g[g.d >= 1].n.sum() / g.n.sum()) for c, g in h.groupby("cat")}
        return {k: float(o.get(k, v)) for k, v in default.items()}
    f = OBSERVED / "origin_delay.csv"
    if not f.exists():
        return dict(default)
    o = pd.read_csv(f).set_index("cat").p_delayed_ge1.to_dict()
    return {k: float(o.get(k, v)) for k, v in default.items()}


NODE_FACTORS = DATA / "node_delay_calibration.csv"
SEG_EXTRA = DATA / "segment_delay_calibration.csv"
OPS_CALIB = DATA / "ops_calibration.csv"
SEG_EXTRA_P = 0.3   # share of block traversals hit by the corridor's extra delay
if "GR_TAU" not in os.environ and OPS_CALIB.exists():     # calibrated node route occupation
    _t = pd.read_csv(OPS_CALIB)
    _t = _t[_t.param == "tau"]
    if len(_t):
        TAU = float(_t.value.iloc[0])


def _seg_extra() -> dict:
    if os.environ.get("GR_SEG_EXTRA", "1") == "0" or not SEG_EXTRA.exists():
        return {}
    f = pd.read_csv(SEG_EXTRA)
    return {r.segment: float(r.extra_min) for r in f.itertuples() if r.extra_min != 0}


def _node_factors() -> dict:
    if os.environ.get("GR_NODE_FACTORS", "1") == "0" or not NODE_FACTORS.exists():
        return {}
    f = pd.read_csv(NODE_FACTORS)
    return {(r.node, r.cat): float(r.factor) for r in f.itertuples()}


def _node_approach() -> dict:
    if os.environ.get("GR_NODE_FACTORS", "1") == "0" or not NODE_FACTORS.exists():
        return {}
    f = pd.read_csv(NODE_FACTORS)
    if "approach" not in f:
        return {}
    return {(r.node, r.cat): float(r.approach) for r in f.itertuples() if r.approach == r.approach and r.approach != 0}


APPROACH_P = 0.3   # share of arrivals hit by the station's approach delay


def _entry_by_node() -> dict:
    """(node, type) -> (share of trains entering late, mean delay if late), observed."""
    f = OBSERVED / "entry_delay_by_node.csv"
    if not f.exists():
        return {}
    e = pd.read_csv(f)
    e = e[e.trains >= 30]
    return {(r.node, r.cat): (float(r.p_delayed_ge1), float(r.mean_if_delayed)) for r in e.itertuples()}


def _ops(key: str, default: dict) -> dict:
    """Calibrated operational parameters (data/ops_calibration.csv: param, cat, value)."""
    out = dict(default)
    if OPS_CALIB.exists():
        f = pd.read_csv(OPS_CALIB)
        for r in f[f.param == key].itertuples():
            out[r.cat] = float(r.value)
    return out


@dataclass
class DisruptionModel:
    # primary (exogenous) delays at train origin: probability, mean (exp. dist.)
    # observed share of trains starting late (DB data) where available, else assumptions
    p_origin: dict = field(default_factory=lambda: _observed_p_origin(
        {"FV": 0.40, "NV": 0.25, "S": 0.20, "GV": 0.50}))
    # observed distribution of origin delays (>= 1 min); categories missing here use exponential
    origin_empirical: dict = field(default_factory=_empirical_origin)
    # observed entry delay per first model node; scales probability and magnitude
    entry_node: dict = field(default_factory=_entry_by_node)
    # per (node, type) multiplier on the dwell extension (calibrated on observed station punctuality)
    node_factor: dict = field(default_factory=_node_factors)
    # per (node, type) approach adjustment in minutes, calibrated on observed station punctuality:
    # > 0: extra delay before the station (approach conflicts, mean per arrival);
    # < 0: delay recovered before the station (running-time supplements before major nodes)
    node_approach: dict = field(default_factory=_node_approach)
    # per corridor: mean extra minutes per traversal (>0: slow orders, construction; <0: faster than
    # modelled), calibrated on observed delay growth between stations (validate.calibrate_segments)
    seg_extra: dict = field(default_factory=_seg_extra)
    mean_origin: dict = field(default_factory=lambda: {"FV": 7.5, "NV": 4.0, "S": 3.0, "GV": 25.0})
    # trains whose vehicle comes from a modelled predecessor get only this share of the observed
    # entry-delay probability (the rest is propagated through the circulation; calibrated)
    p_pred_scale: dict = field(default_factory=lambda: _ops("p_pred_scale", {"FV": 0.5, "NV": 0.5, "S": 0.5}))
    # delay recovered in a turnaround beyond the planned buffer (quick turns, swaps; calibrated)
    turn_recovery: dict = field(default_factory=lambda: _ops("turn_recovery", {"FV": 0.0, "NV": 0.0, "S": 0.0}))
    # dwell-time extensions at each scheduled stop
    p_dwell: dict = field(default_factory=lambda: {"FV": 0.30, "NV": 0.30, "S": 0.08})
    mean_dwell: dict = field(default_factory=lambda: {"FV": 3.0, "NV": 1.1, "S": 0.8})
    # running-time variation (multiplicative, normal, sd)
    run_sd: float = 0.03
    # infrastructure incidents, per km per day
    rate_track_fault: float = 1.0e-3    # one track blocked (signal, switch, track defect) per track-km
    rate_full_closure: float = 1.5e-4   # all tracks blocked (person/police/fire/tree) per route-km
    median_track_fault: float = 45.0    # min, lognormal
    median_full_closure: float = 75.0
    sigma_duration: float = 0.8
    # vehicle failures (calibrated on the observed cancellations attributed to vehicles)
    p_fail_pre: dict = field(default_factory=lambda: _ops("p_fail_pre", {"FV": 0.008, "NV": 0.008, "S": 0.008}))
    fail_per_km: dict = field(default_factory=lambda: _ops("fail_per_km", {"FV": 4e-5, "NV": 4e-5, "S": 4e-5}))
    median_breakdown: float = 40.0      # min a broken-down train blocks its track
    scale: float = 1.0                  # global multiplier on exogenous train delays
    f_fail: float = 1.0                 # global multiplier on vehicle failures (lever / sensitivity)

    def __post_init__(self):
        # observed national entry statistics, used to rescale node-specific values
        self._p_obs, self._mean_obs = {}, {}
        for c, (vals, w) in self.origin_empirical.items():
            self._mean_obs[c] = float(np.average(vals, weights=w))
        f = OBSERVED / "entry_delay_hist.csv"
        if f.exists():
            h = pd.read_csv(f)
            for c, g in h.groupby("cat"):
                self._p_obs[c] = float(g[g.d >= 1].n.sum() / g.n.sum())

    def to_dict(self):
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.__dict__.items()
                if k not in ("origin_empirical", "node_factor", "entry_node", "_p_obs", "_mean_obs",
                             "seg_extra", "node_approach")}


# ----------------------------------------------------------------------------
class Channel:
    __slots__ = ("dir", "entries", "exits", "dirs", "hws", "blocked", "bidir", "exclusive")

    def __init__(self, d, exclusive=False):
        self.dir = d
        self.entries: list[float] = []
        self.exits: list[float] = []
        self.dirs: list[int] = []
        self.hws: list[float] = []      # headway of each occupant as a follower
        self.blocked: list[tuple[float, float]] = []
        self.bidir: list[tuple[float, float]] = []
        self.exclusive = exclusive      # platform track / throat route group: one train at a time

    def usable(self, d: int, t: float) -> bool:
        if self.dir == 0 or self.dir == d:
            return True
        for a, b in self.bidir:
            if a <= t < b:
                return True
        return False

    def fit(self, t: float, r: float, d: int, h: float) -> float:
        """Earliest entry >= t. ``h``: headway of the new train as a follower (for exclusive
        channels: the buffer between two occupations)."""
        entries = self.entries
        for _ in range(10000):
            moved = False
            for b0, b1 in self.blocked:
                if t < b1 and t + r > b0:
                    t = b1
                    moved = True
            lo = bisect_left(entries, t - LOOKBACK)
            hi = bisect_right(entries, t + r + max(h, 3.0))
            if self.exclusive:
                for i in range(lo, hi):
                    e, x = entries[i], self.exits[i]
                    if t < x + h and t + r + h > e:
                        t = x + h
                        moved = True
                        break
            else:
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
                        else:
                            hf = self.hws[i]          # the existing train follows the new one
                            if t + hf > e or t + r + hf > x:
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

    def book(self, t: float, r: float, d: int, h: float = 3.0):
        i = bisect_right(self.entries, t)
        self.entries.insert(i, t)
        self.exits.insert(i, t + r)
        self.dirs.insert(i, d)
        self.hws.insert(i, h)

    def unbook(self, t: float, r: float, d: int):
        i = bisect_left(self.entries, t)
        while i < len(self.entries):
            if self.entries[i] == t and self.exits[i] == t + r and self.dirs[i] == d:
                del self.entries[i], self.exits[i], self.dirs[i], self.hws[i]
                return
            i += 1
        raise KeyError("occupation not found")


def _sample_entry_delay(dm, rng, node, cat):
    """Delay of a train entering the model at ``node`` (observed distribution, node-scaled)."""
    en = dm.entry_node.get((node, cat))
    p0 = dm.p_origin.get(cat, 0.0)
    p_in = p0 * en[0] / dm._p_obs.get(cat, p0) if en and p0 > 0 else p0
    if rng.random() >= min(p_in, 0.99):
        return 0.0
    emp = dm.origin_empirical.get(cat)
    if emp:
        mag = en[1] / dm._mean_obs[cat] if en and cat in dm._mean_obs else 1.0
        return rng.choices(emp[0], weights=emp[1])[0] * dm.scale * mag
    return rng.expovariate(1.0 / (dm.mean_origin.get(cat, 4.0) * dm.scale))


_BACKGROUND: dict = {}
TIMETABLES = DATA / "timetables"


def node_background(path=None):
    path = str(path or DATA / "node_background.csv")
    if path not in _BACKGROUND:
        f = Path(path)
        _BACKGROUND[path] = pd.read_csv(f) if f.exists() else pd.DataFrame(
            columns=["node", "category", "arr_min", "dep_min"])
    return _BACKGROUND[path]


def month_timetable(month: str):
    """(timetable csv, background csv) of a monthly timetable built from the DB planned times."""
    return TIMETABLES / f"tt_{month}.csv", TIMETABLES / f"bg_{month}.csv"


class Infra:
    """Block/channel state (corridors and major nodes) for one planning or simulation run."""

    def __init__(self, net: Network, plan: bool = False, dm=None, seed=None):
        self.net = net
        self.plan = plan
        self.blocks: dict[tuple[str, int], list[Channel]] = {}
        self.buf = PLAN_BUFFER if plan else 0.0
        self.nbuf = PLAN_NODE_BUFFER if plan else 0.0
        self.hpair: dict[str, tuple[float, float]] = {}
        for s in net.segments.values():
            self.hpair[s.id] = (s.hw(False) + self.buf, s.hw(True) + self.buf)
            for bi in range(s.n_blocks()):
                self.blocks[(s.id, bi)] = [Channel(d) for d in channel_layout(s.tracks)]
        for n, cap in net.node_cap.items():
            self.blocks[("@P:" + n, 0)] = [Channel(0, True) for _ in range(cap.platforms)]
            self.blocks[("@T:" + n, 0)] = [Channel(0, True) for _ in range(cap.groups)]
        self.full_closures: dict[str, list[tuple[float, float]]] = {}
        self._book_background(dm, seed)

    def h(self, sid: str, equipped: bool) -> float:
        return self.hpair[sid][1 if equipped else 0]

    def _book_background(self, dm=None, seed=None):
        """Trains touching only one node occupy its platforms and throat. In operations (dm, seed given)
        they arrive with the observed delay of trains entering the model at that node."""
        bg = node_background(self.net.bg_file)
        caps = self.net.node_cap
        for k, r in enumerate(bg[bg.node.isin(caps)].itertuples()):
            a, d = float(r.arr_min), float(r.dep_min)
            occ = max(d - a, 0.0)
            if occ < 2.0:   # first/last call: platform used for preparation / turning
                a, occ = a - PLATFORM_PREP.get(r.category, 6.0) / 2, PLATFORM_PREP.get(r.category, 6.0)
            if dm is not None and seed is not None:
                a += _sample_entry_delay(dm, random.Random(crc(seed, "bg", k)), r.node, r.category)
            tau = TAU
            P = self.blocks[("@P:" + r.node, 0)]
            T = self.blocks[("@T:" + r.node, 0)]
            t0, ch = self._earliest_excl(P, a, occ + tau, 0.0)
            ch.book(t0, occ + tau, 0)
            for tt in (t0 - tau, t0 + occ):
                t1, c2 = self._earliest_excl(T, tt, tau, 0.0)
                c2.book(t1, tau, 0)

    @staticmethod
    def _earliest_excl(chans, t, r, buf, pref=None):
        if pref is not None and pref < len(chans):
            ft = chans[pref].fit(t, r, 0, buf)
            if ft <= t + 1e-9:
                return ft, chans[pref]
        best_t, best_c = math.inf, None
        for ch in chans:
            ft = ch.fit(t, r, 0, buf)
            if ft < best_t - 1e-9:
                best_t, best_c = ft, ch
                if ft <= t + 1e-9:
                    break
        return best_t, best_c

    def channel_for(self, key, d, t, pref):
        chans = self.blocks[key]
        if pref is not None and pref < len(chans) and chans[pref].usable(d, t):
            return chans[pref]
        for c in chans:
            if c.usable(d, t):
                return c
        return chans[0]

    def earliest(self, key, t, r, d, prefer_slow: bool, pref: int | None = None, equipped: bool = False):
        chans = self.blocks[key]
        h = self.hpair[key[0]][1 if equipped else 0]
        if pref is not None and pref < len(chans):
            ch = chans[pref]
            if ch.usable(d, t):
                ft = ch.fit(t, r, d, h)
                if ft <= t + 1e-9:
                    return ft, ch, h
        order = range(len(chans) - 1, -1, -1) if prefer_slow else range(len(chans))
        best_t, best_c = math.inf, None
        for ci in order:
            ch = chans[ci]
            if not ch.usable(d, t):
                continue
            ft = ch.fit(t, r, d, h)
            if ft < best_t - 1e-9:
                best_t, best_c = ft, ch
        if best_c is None:
            for ch in chans:
                ft = ch.fit(t, r, d, h)
                if ft < best_t:
                    best_t, best_c = ft, ch
        return best_t, best_c, h

    # -- nodes --------------------------------------------------------------
    def tau(self, node: str, equipped: bool) -> float:
        cap = self.net.node_cap[node]
        return TAU * (TAU_ETCS_FACTOR if (cap.etcs and equipped) else 1.0)

    def node_arrive(self, node, t, dur_fn, tau, pref=None):
        """Train reaches the station entrance at t. Returns (platform arrival, bookings): throat route
        [tp - tau, tp] and platform [tp, tp + dur_fn(tp)] both free."""
        P = self.blocks[("@P:" + node, 0)]
        T = self.blocks[("@T:" + node, 0)]
        tp = t
        pt, pp = (pref("@T:" + node), pref("@P:" + node)) if pref else (None, None)
        for _ in range(30):
            t1, tch = self._earliest_excl(T, tp - tau, tau, self.nbuf, pt)
            tp = max(tp, t1 + tau)
            dur = dur_fn(tp)
            t2, pch = self._earliest_excl(P, tp, dur, self.nbuf, pp)
            if t2 <= tp + 1e-9:
                tch.book(tp - tau, tau, 0)
                pch.book(tp, dur, 0)
                return tp, [(tch, tp - tau, tau), (pch, tp, dur)]
            tp = t2
        tch.book(tp - tau, tau, 0)
        pch.book(tp, dur, 0)
        return tp, [(tch, tp - tau, tau), (pch, tp, dur)]

    def node_leave(self, node, t, tau, pref=None):
        T = self.blocks[("@T:" + node, 0)]
        t1, tch = self._earliest_excl(T, t, tau, self.nbuf, pref("@T:" + node) if pref else None)
        tch.book(t1, tau, 0)
        return t1, [(tch, t1, tau)]

    def node_pass(self, node, t, tau, pref=None):
        return self.node_leave(node, t, tau, pref)

    def node_origin(self, node, t, prep, tau, pref=None):
        """Originating train: platform [t - prep, t] and throat route [t, t + tau]."""
        P = self.blocks[("@P:" + node, 0)]
        T = self.blocks[("@T:" + node, 0)]
        td = t
        pp, pt = (pref("@P:" + node), pref("@T:" + node)) if pref else (None, None)
        for _ in range(30):
            t2, pch = self._earliest_excl(P, td - prep, prep + tau, self.nbuf, pp)
            td = max(td, t2 + prep)
            t1, tch = self._earliest_excl(T, td, tau, self.nbuf, pt)
            if t1 <= td + 1e-9:
                break
            td = t1
        pch.book(td - prep, prep + tau, 0)
        tch.book(td, tau, 0)
        return td, [(pch, td - prep, prep + tau), (tch, td, tau)]

    # -- incidents --------------------------------------------------------
    def add_track_fault(self, sid, bi, track_idx, t0, t1, ch=None):
        chans = self.blocks[(sid, bi)]
        ch = ch or chans[track_idx % len(chans)]
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
    pax: float = 0.0                       # expected passengers on board
    steps: list = field(default_factory=list)  # [seg_id, block_idx, dir, run, end_node, is_stop]
    plan_entry: list = field(default_factory=list)
    plan_arr: list = field(default_factory=list)
    plan_ch: list = field(default_factory=list)
    plan_nodes: list = field(default_factory=list)   # planned node occupations (key, ch idx, t, r)
    plan_dep0: float = -math.inf           # planned departure from the origin node (after platform/throat)
    hold: dict = field(default_factory=dict)   # node -> earliest scheduled departure (GTFS)
    line: str = ""
    agency: str = ""
    group: str = ""
    equipped: bool = False
    sched_arr: float = 0.0                 # scheduled arrival at the last stop (GTFS)
    pred: int | None = None                # circulation predecessor (index)
    pred_kind: str | None = None
    turn: float = 0.0
    hw_next: float = 60.0

    def stop_weight(self) -> float:
        """Passengers alighting per scheduled stop after the origin."""
        if self.ttype == "GV":
            return FREIGHT_PAX_EQUIV
        n_alight = max(1, len(self.stops) - 1) if self.ttype == "FV" else 1
        return self.pax / n_alight


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


TIMETABLE = os.environ.get("GR_TIMETABLE", "gtfs")   # "gtfs" (real timetable) or "curated"
PAX_FV_LINE = {"ICE": 350.0, "ECE": 300.0, "RJ": 250.0, "RJX": 250.0}


def build_trains(net: Network, data_dir=DATA, source: str | None = None, shifts: dict | None = None,
                 tt_file=None) -> list[Train]:
    """Trains of the timetable. ``shifts`` (train id -> minutes) moves trains (timetable softening).
    ``tt_file``: another timetable in the same format (e.g. a monthly timetable from the DB data)."""
    source = source or TIMETABLE
    if tt_file is not None:
        source = "gtfs"
    if source == "gtfs" and (tt_file is not None or (data_dir / "timetable_gtfs.csv").exists()):
        trains = build_trains_gtfs(net, data_dir, shifts, tt_file)
    else:
        trains = build_trains_curated(net, data_dir)
        for tr in trains:
            tr.group = fleet.raw_group(tr.ttype, "", "")
    fleet.assign_equipment(net, trains)
    if source == "gtfs":
        fleet.build_circulations(trains)
    return trains


def _freight(net: Network, data_dir, shifts=None) -> list[Train]:
    trains = []
    gv = pd.read_csv(data_dir / "freight_flows.csv")
    for r in gv.itertuples():
        via = r.via.split()
        n = int(r.trains_per_day_per_dir)
        for direction, seq in (("a", via), ("b", via[::-1])):
            gap = 1440.0 / n
            off = (crc(r.id, direction) % 1000) / 1000 * gap
            for k in range(n):
                tid = f"{r.id}-{direction}-{k}"
                tr = Train(tid, "GV", 100.0, off + k * gap + (shifts or {}).get(tid, 0.0), [seq[0], seq[-1]],
                           group="Güterverkehr")
                try:
                    route_train(net, tr, seq)
                except ValueError:      # corridor closed in this scenario
                    continue
                trains.append(tr)
    return trains


def build_trains_gtfs(net: Network, data_dir=DATA, shifts: dict | None = None, tt_file=None) -> list[Train]:
    """Passenger trains from the real GTFS timetable (data/timetable_gtfs.csv) + curated freight.

    S-Bahn trips are kept only where S-Bahn shares main-line tracks (segments with s_tph > 0
    in segments.csv); S-Bahn networks on their own tracks (Hamburg, Berlin, München, ...) are dropped.
    """
    trains: list[Train] = []
    shifts = shifts or {}
    major = net.nodes["major"].to_dict()
    tt = pd.read_csv(tt_file or data_dir / "timetable_gtfs.csv", keep_default_na=False)
    info = {}
    for r in tt.itertuples():
        stops = r.stops.split()
        sh = shifts.get(r.train_id, 0.0)
        deps = [float(x) + sh for x in str(r.dep_min).split()]
        arrs = [float(x) + sh for x in str(r.arr_min).split()]
        cat = r.category
        if cat == "FV":
            prefix = "".join(ch for ch in str(r.line) if ch.isalpha()).upper()
            vmax = 300.0 if prefix == "ICE" else 230.0 if prefix.startswith("RJ") else 200.0
            pax = PAX_FV_LINE.get(prefix, 250.0)
        else:
            vmax = 160.0 if cat == "NV" else 120.0
            pax = PAX_DEFAULT[cat] * (1.0 + 0.25 * (major[stops[0]] + major[stops[-1]]) / 2)
        tr = Train(r.train_id, cat, vmax, deps[0], stops, pax=pax * hour_factor(deps[0]),
                   hold=dict(zip(stops[1:-1], deps[1:])), line=str(r.line),
                   agency=str(getattr(r, "agency", "")), sched_arr=arrs[-1])
        try:
            route_train(net, tr, stops)
        except ValueError:
            continue
        if cat == "S" and any(net.segments[st[0]].s_tph <= 0 for st in tr.steps):
            continue
        trains.append(tr)
        info[tr.id] = (tr.line, tr.agency)
    fleet.assign_groups(trains, info)
    return trains + _freight(net, data_dir, shifts)


def build_trains_curated(net: Network, data_dir=DATA) -> list[Train]:
    trains: list[Train] = []
    major = net.nodes["major"].to_dict()
    fv = pd.read_csv(data_dir / "fv_lines.csv")
    for r in fv.itertuples():
        stops = r.stops.split()
        off = crc(r.id) % 30
        t0, t1 = hm(r.first_dep) + off, hm(r.last_dep) + off
        pax = float(getattr(r, "pax", PAX_DEFAULT["FV"]))
        for direction, seq in (("a", stops), ("b", stops[::-1])):
            k = 0
            t = t0 + (0 if direction == "a" else 7)
            while t <= t1 + 7:
                tr = Train(f"{r.id}-{direction}-{k}", "FV", float(r.vmax), t, seq,
                           pax=pax * hour_factor(t))
                route_train(net, tr, seq)
                trains.append(tr)
                t += r.headway_min
                k += 1
    trains += _freight(net, data_dir)
    for s in net.segments.values():
        metro = 1.0 + 0.25 * (major[s.a] + major[s.b])
        for ttype, tph, vmax in (("NV", s.nv_tph, 160.0), ("S", s.s_tph, 120.0)):
            if tph <= 0 or ttype not in s.types:
                continue
            hw = 60.0 / tph
            for direction, (a, b) in (("a", (s.a, s.b)), ("b", (s.b, s.a))):
                off = (crc(s.id, ttype, direction) % 1000) / 1000 * hw
                t, k = 300.0 + off, 0
                while t < 1440:
                    tr = Train(f"{ttype}-{s.id}-{direction}-{k}", ttype, vmax, t, [a, b],
                               pax=PAX_DEFAULT[ttype] * metro * hour_factor(t))
                    d = 1 if a == s.a else -1
                    tr.steps = expand_path(net, [(s.id, d)], ttype, vmax, {b})
                    trains.append(tr)
                    t += hw
                    k += 1
    return trains


# ----------------------------------------------------------------------------
def _step_keys(tr: Train):
    return [(st[0], st[1], st[2]) for st in tr.steps]


def _node_of_step(net, tr, st):
    """Node at the end of a step that has a node model and is relevant for this passenger train."""
    n = st[4]
    if n is None or tr.ttype == "GV" or n not in net.node_cap:
        return None
    return n


def plan_timetable(net: Network, trains: list[Train], base: dict | None = None) -> dict:
    """Conflict-free timetable (corridors and major nodes).

    Without ``base``: every train is planned by priority (ICE, RE, S/tram, cargo), then time.
    With ``base`` (train id -> planned Train of the current timetable): trains whose
    block sequence, departure and equipment are unchanged keep their current slots exactly; only
    the others (new route, shifted, ...) are re-planned into the gaps.
    """
    infra = Infra(net, plan=True)
    to_plan = []
    n_fixed = 0
    for tr in trains:
        b = base.get(tr.id) if base else None
        if (b is not None and _step_keys(b) == _step_keys(tr) and b.desired_dep == tr.desired_dep
                and b.plan_nodes is not None
                and all(k[0][3:] in net.node_cap for k, *_ in b.plan_nodes)):
            tr.plan_entry, tr.plan_arr = list(b.plan_entry), list(b.plan_arr)
            tr.plan_ch = []
            for (sid, bi, d, *_), te, ta, ci in zip(tr.steps, b.plan_entry, b.plan_arr, b.plan_ch):
                ch = infra.channel_for((sid, bi), d, te, ci)
                ch.book(te, ta - te, d, infra.h(sid, tr.equipped))
                tr.plan_ch.append(infra.blocks[(sid, bi)].index(ch))
            tr.plan_nodes = list(b.plan_nodes)
            tr.plan_dep0 = b.plan_dep0
            for key, ci, t0, r in tr.plan_nodes:
                chans = infra.blocks[key]
                chans[min(ci, len(chans) - 1)].book(t0, r, 0)
            n_fixed += 1
        else:
            to_plan.append(tr)
    order = sorted(to_plan, key=lambda tr: (PRIORITY[tr.ttype], tr.desired_dep))
    planned_wait = 0.0

    def rec(tr, bookings):
        for ch, t0, r in bookings:
            tr.plan_nodes.append((ch_key[id(ch)], ch_idx[id(ch)], t0, r))

    ch_key, ch_idx = {}, {}
    for key, chans in infra.blocks.items():
        if key[0][0] == "@":
            for i, ch in enumerate(chans):
                ch_key[id(ch)] = key
                ch_idx[id(ch)] = i

    for tr in order:
        supp = 1.0 + SUPPLEMENT[tr.ttype]
        t = tr.desired_dep
        tr.plan_entry, tr.plan_arr, tr.plan_ch, tr.plan_nodes = [], [], [], []
        slow = tr.ttype != "FV"
        o = tr.stops[0]
        if tr.ttype != "GV" and o in net.node_cap:
            t, bk = infra.node_origin(o, t, PLATFORM_PREP[tr.ttype], infra.tau(o, tr.equipped))
            rec(tr, bk)
        tr.plan_dep0 = t
        n_steps = len(tr.steps)
        for i, (sid, bi, d, r, end_node, is_stop) in enumerate(tr.steps):
            rp = r * supp
            te, ch, h = infra.earliest((sid, bi), t, rp, d, slow, None, tr.equipped)
            planned_wait += te - t
            ch.book(te, rp, d, h)
            tr.plan_entry.append(te)
            tr.plan_ch.append(infra.blocks[(sid, bi)].index(ch))
            t = te + rp
            node = _node_of_step(net, tr, tr.steps[i])
            if is_stop:
                last = i == n_steps - 1
                hold = tr.hold.get(end_node) if tr.hold else None
                if node:
                    tau = infra.tau(node, tr.equipped)
                    if last:
                        dur_fn = lambda tp: PLATFORM_TURN[tr.ttype]
                    else:
                        dur_fn = lambda tp, hold=hold: max(DWELL[tr.ttype], (hold or 0) - tp) + tau
                    t, bk = infra.node_arrive(node, t, dur_fn, tau)
                    rec(tr, bk)
                tr.plan_arr.append(t)
                if not last:
                    t += DWELL[tr.ttype]
                    if hold is not None:
                        t = max(t, hold)
                    if node:
                        t, bk = infra.node_leave(node, t, tau)
                        rec(tr, bk)
            else:
                if node:
                    t2, bk = infra.node_pass(node, t, infra.tau(node, tr.equipped))
                    rec(tr, bk)
                    t = max(t, t2)
                tr.plan_arr.append(t)
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


ARR_BINS = (5.0, 10.0, 20.0, 40.0)
ARR_BIN_LABELS = ("0-5", "5-10", "10-20", "20-40", ">40")


def _arr_bin(d):
    for b, lab in zip(ARR_BINS, ARR_BIN_LABELS):
        if d <= b:
            return lab
    return ARR_BIN_LABELS[-1]


def simulate_day(net: Network, trains: list[Train], dm: DisruptionModel, seed: int,
                 reroute: bool = True, priority: bool = True, trace: list | None = None) -> dict:
    """Simulate one operating day. ``trace`` (optional list) receives one tuple per
    block occupation: (train_idx, entry, exit, seg_id, block, dir, delay_vs_plan)."""
    infra = Infra(net, dm=dm, seed=seed)
    incidents = sample_incidents(net, dm, seed)
    for kind, sid, bi, k, t0, t1 in incidents:
        if kind == "full":
            infra.add_full_closure(sid, bi, t0, t1)
        else:
            infra.add_track_fault(sid, bi, k, t0, t1)

    pq = []
    state = {}
    # dispatcher look-ahead: block key -> {train idx: (ready, run, dir, prio, pref_channel, headway)}
    incoming: dict[tuple, dict[int, tuple]] = {}

    def announce(idx, i, ready):
        tr = trains[idx]
        _, steps, pe, _, pc = state[idx]
        sid, bi, d, r, *_ = steps[i]
        incoming.setdefault((sid, bi), {})[idx] = (max(ready, pe[i]), r, d, PRIORITY[tr.ttype], pc[i],
                                                   infra.h(sid, tr.equipped))

    # vehicle circulation state
    succ_of = {}
    for idx, tr in enumerate(trains):
        if tr.pred is not None:
            succ_of[tr.pred] = idx
    arrived: dict[int, tuple[float, float]] = {}    # idx -> (arrival time, arrival delay)
    cancelled: set = set()
    waiting: dict[int, tuple[int, float]] = {}      # pred idx -> (succ idx, check time)
    last_delay: dict[int, float] = {}
    reserve_units = {k: [0.0] * n for k, n in net.reserves.items() if n > 0}
    swapped_pred: dict[int, tuple] = {}             # pred idx whose vehicle is replaced -> (pool, unit)
    n_reserve_used = {"late": 0, "fail": 0}

    node_q: dict[int, dict] = {}

    def prefs(idx):
        """Planned platform / route-group per node visit, consumed in booking order."""
        q = node_q.get(idx)
        if q is None:
            q = {}
            for key, ci, *_ in trains[idx].plan_nodes or ():
                q.setdefault(key[0], []).append(ci)
            for k in q:
                q[k].reverse()
            node_q[idx] = q
        return lambda k: (q[k].pop() if q.get(k) else None)

    start_check: dict[int, float] = {}
    for idx, tr in enumerate(trains):
        rng = random.Random(crc(seed, tr.id))
        prim = 0.0
        en = dm.entry_node.get((tr.stops[0], tr.ttype))
        p0 = dm.p_origin[tr.ttype]
        p_in = p0 * en[0] / dm._p_obs.get(tr.ttype, p0) if en else p0   # keeps lever scaling of p_origin
        if tr.pred is not None:
            p_in *= dm.p_pred_scale.get(tr.ttype, 1.0)
        if rng.random() < min(p_in, 0.99):
            emp = dm.origin_empirical.get(tr.ttype)
            if emp:
                mag = en[1] / dm._mean_obs[tr.ttype] if en and tr.ttype in dm._mean_obs else 1.0
                prim = rng.choices(emp[0], weights=emp[1])[0] * dm.scale * mag
            else:
                prim = rng.expovariate(1.0 / (dm.mean_origin[tr.ttype] * dm.scale))
        state[idx] = (rng, [list(s) for s in tr.steps], list(tr.plan_entry),
                      list(tr.plan_arr), list(tr.plan_ch))
        t_check = tr.desired_dep + prim
        start_check[idx] = t_check
        heapq.heappush(pq, (t_check, idx, -1))

    delay = dict.fromkeys(TYPES, 0.0)
    pax_delay = dict.fromkeys(TYPES, 0.0)
    pax_cancel = dict.fromkeys(TYPES, 0.0)
    pax_total = dict.fromkeys(TYPES, 0.0)
    pax_ontime = dict.fromkeys(TYPES, 0.0)
    n_stops = dict.fromkeys(TYPES, 0)
    n_punct = dict.fromkeys(TYPES, 0)
    visits = dict.fromkeys(TYPES, 0)        # stop-node visits incl. origin (served or cancelled)
    canc_visits = {k: [0, 0] for k in TYPES}   # [full, tail]
    n_cancel = {k: [0, 0] for k in TYPES}
    starts = {k: [0, 0] for k in TYPES}     # [n, n with entry delay >= 1]
    turn_stats: dict[tuple, list] = {}      # (cat, arr bin) -> [n, sum dep delay, n>=3, n>=6]
    node_stats: dict[tuple[str, str], list] = {}   # (node, type) -> [stops, on time, delay sum]
    node_dwell: dict[tuple[str, str], list] = {}   # (node, type) -> [n, sum(dep delay - arr delay)]
    leg_stats: dict[tuple, list] = {}               # (from node, to node, type) -> [n, sum delay change]
    dep_info: dict[int, tuple] = {}
    arr_info: dict[int, tuple] = {}
    final_delay = dict.fromkeys(TYPES, 0.0)
    seg_wait: dict[str, float] = {}
    node_wait: dict[str, float] = {}
    n_reroutes = 0
    n_priority_holds = 0

    def cancel(idx, from_step, extra, kind):
        """Cancel the rest of a trip from step index from_step (0 = whole trip)."""
        tr = trains[idx]
        cancelled.add(idx)
        if tr.ttype == "GV":
            return
        steps = state[idx][1]
        k = 0 if kind == "full" else 1
        n_cancel[tr.ttype][k] += 1
        n_v = 1 if from_step == 0 else 0
        pen = min(CANCEL_CAP, tr.hw_next + extra)
        w = tr.stop_weight()
        for j in range(from_step, len(steps)):
            if steps[j][5]:
                n_v += 1
                last = j == len(steps) - 1
                if tr.ttype == "FV" or last:
                    pax_cancel[tr.ttype] += pen * w
        canc_visits[tr.ttype][k] += n_v
        visits[tr.ttype] += n_v
        release_succ(idx)

    def release_succ(idx):
        """The vehicle of idx will not arrive (cancelled): its successor starts on its own."""
        if idx in waiting:
            s, tc = waiting.pop(idx)
            heapq.heappush(pq, (tc, s, 0))
        if idx in swapped_pred:      # the reserve set taken over is not returned
            swapped_pred.pop(idx)

    def free_unit(key, t):
        units = reserve_units.get(key)
        if not units:
            return None
        for u, ft in enumerate(units):
            if ft <= t:
                return u
        return None

    def ready_after_pred(tr, p_arr, p_dl):
        """Earliest start given the predecessor's arrival delay: the planned turnaround buffer above
        the minimum turnaround absorbs delay (next piece of the same trip: 7% recovery off-model)."""
        p0 = tr.plan_dep0 if tr.plan_dep0 > -math.inf else (tr.plan_entry[0] if tr.plan_entry else tr.desired_dep)
        if tr.pred_kind == "cont":
            return p0 + max(0.0, p_dl - 0.07 * tr.turn)
        ptr = trains[tr.pred]
        p_plan_arr = ptr.plan_arr[-1] if ptr.plan_arr else ptr.sched_arr
        buf = max(0.0, p0 - p_plan_arr - fleet.MIN_TURN[tr.ttype])
        return p0 + max(0.0, p_dl - buf - dm.turn_recovery.get(tr.ttype, 0.0))

    while pq:
        t, idx, i = heapq.heappop(pq)
        tr = trains[idx]
        if idx in cancelled:
            continue
        rng, steps, plan_entry, plan_arr, plan_ch = state[idx]

        # ---- start check: vehicle availability, failures before departure, hot reserves --------
        if i == -1:
            if tr.ttype != "GV":
                cat = tr.ttype
                key = (tr.stops[0], tr.group)
                if rng.random() < dm.p_fail_pre.get(cat, 0.0) * dm.f_fail:
                    u = free_unit(key, t)
                    if u is not None:
                        reserve_units[key][u] = math.inf
                        n_reserve_used["fail"] += 1
                        if tr.pred is not None and tr.pred not in arrived and tr.pred not in cancelled:
                            swapped_pred[tr.pred] = None
                        heapq.heappush(pq, (t + SWAP_DELAY_FAIL[cat], idx, 0))
                    else:
                        cancel(idx, 0, 0.0, "full")
                    continue
                p = tr.pred
                if p is not None and p not in cancelled:
                    if p in arrived:
                        ready = ready_after_pred(tr, *arrived[p])
                        if ready > t + LATE_SWAP[cat]:
                            u = free_unit(key, t)
                            if u is not None:
                                reserve_units[key][u] = arrived[p][0] + fleet.MIN_TURN[cat]
                                n_reserve_used["late"] += 1
                                heapq.heappush(pq, (t + SWAP_DELAY_LATE, idx, 0))
                                continue
                        heapq.heappush(pq, (max(t, ready), idx, 0))
                        continue
                    ptr = trains[p]
                    proj_arr = (ptr.plan_arr[-1] if ptr.plan_arr else ptr.sched_arr) + last_delay.get(p, 0.0)
                    proj_ready = ready_after_pred(tr, proj_arr, last_delay.get(p, 0.0))
                    if proj_ready > t + LATE_SWAP[cat]:
                        u = free_unit(key, t)
                        if u is not None:
                            reserve_units[key][u] = math.inf
                            swapped_pred[p] = (key, u)
                            n_reserve_used["late"] += 1
                            heapq.heappush(pq, (t + SWAP_DELAY_LATE, idx, 0))
                            continue
                    waiting[p] = (idx, t)
                    continue
            heapq.heappush(pq, (t, idx, 0))
            continue

        sid, bi, d, r, end_node, is_stop = steps[i]
        key = (sid, bi)
        prio = PRIORITY[tr.ttype]
        slow = tr.ttype != "FV"
        eq = tr.equipped
        ready = max(t, plan_entry[i])
        if i == 0:
            o = tr.stops[0]
            if tr.ttype != "GV" and o in net.node_cap:
                t0 = max(t, tr.plan_dep0)
                td, _ = infra.node_origin(o, t0, PLATFORM_PREP[tr.ttype], infra.tau(o, eq), prefs(idx))
                if td > t0:
                    node_wait[o] = node_wait.get(o, 0.0) + td - t0
                ready = max(td, plan_entry[0])
        ract = r * max(0.97, 1.0 + rng.gauss(0, dm.run_sd))
        m_extra = dm.seg_extra.get(sid, 0.0)
        nbk = net.segments[sid].n_blocks()
        if m_extra > 0:   # slow orders / construction on this corridor (calibrated, per block)
            if rng.random() < SEG_EXTRA_P:
                ract += rng.expovariate(SEG_EXTRA_P * nbk / m_extra)
        elif m_extra < 0:  # trains run faster than modelled (larger real recovery margin)
            ract = max(0.85 * r, ract + m_extra / nbk)

        ghosts = []
        if priority:
            inc = incoming.get(key)
            if inc:
                inc.pop(idx, None)
                for gidx, (gt, gr, gd, gp, gpc, gh) in inc.items():
                    if gp < prio and ready - 1e-9 <= gt <= ready + LOOKAHEAD:
                        gch = infra.channel_for(key, gd, gt, gpc)
                        gch.book(gt, gr, gd, gh)
                        ghosts.append((gch, gt, gr, gd))
        te, ch, h = infra.earliest(key, ready, ract, d, slow, plan_ch[i], eq)
        if ghosts:
            for gch, gt, gr, gd in ghosts:
                gch.unbook(gt, gr, gd)
            te_free, ch_free, _ = infra.earliest(key, ready, ract, d, slow, plan_ch[i], eq)
            if te > te_free + 1e-9:
                n_priority_holds += 1
                # held for the higher-priority train; may still use any track free at te
                te, ch, h = infra.earliest(key, te, ract, d, slow, plan_ch[i], eq)
            else:
                te, ch = te_free, ch_free

        if reroute and tr.ttype in ("FV", "GV") and te - ready > REROUTE_WAIT and sid in infra.full_closures:
            new = _try_reroute(net, infra, tr, steps, i, ready, te, plan_entry, plan_arr, plan_ch)
            if new is not None:
                steps, plan_entry, plan_arr, plan_ch = new
                state[idx] = (rng, steps, plan_entry, plan_arr, plan_ch)
                n_reroutes += 1
                heapq.heappush(pq, (ready, idx, i))
                if priority:
                    announce(idx, i, ready)
                continue

        ch.book(te, ract, d, h)
        if i == 0:
            if plan_entry[0] > -math.inf:
                starts[tr.ttype][0] += 1
                starts[tr.ttype][1] += (te - plan_entry[0]) >= 1.0
                if tr.pred_kind == "turn" and tr.pred in arrived:
                    b = (tr.ttype, _arr_bin(max(0.0, arrived[tr.pred][1])))
                    s_ = turn_stats.setdefault(b, [0, 0.0, 0, 0])
                    dd = te - plan_entry[0]
                    s_[0] += 1
                    s_[1] += dd
                    s_[2] += dd >= 3
                    s_[3] += dd >= 6
            if tr.ttype != "GV":
                visits[tr.ttype] += 1
        if trace is not None:
            pe_i = plan_entry[i]
            trace.append((idx, te, te + ract, sid, bi, d, te - pe_i if pe_i > -math.inf else math.nan))
        wait = te - ready
        if wait > 0:
            seg_wait[sid] = seg_wait.get(sid, 0.0) + wait
        if plan_entry[i] > -math.inf:
            last_delay[idx] = te - plan_entry[i]
        tx = te + ract

        # vehicle breakdown en route: the train blocks its track, the rest of the trip is cancelled
        if tr.ttype != "GV":
            pf = dm.fail_per_km.get(tr.ttype, 0.0) * dm.f_fail * net.segments[sid].length / nbk
            if pf > 0 and rng.random() < pf:
                tf = te + ract * rng.random()
                dur = dm.median_breakdown * math.exp(rng.gauss(0, 0.6))
                infra.add_track_fault(sid, bi, 0, tf, tf + dur, ch=ch)
                cancel(idx, i, dur, "tail")
                continue

        nxt = tx
        if (i == 0 or steps[i - 1][5]) and plan_entry[i] > -math.inf:
            dep_info[idx] = (tr.stops[0] if i == 0 else steps[i - 1][4], te - plan_entry[i])
            if idx in arr_info:
                a_node, a_dl = arr_info.pop(idx)
                nd = node_dwell.setdefault((a_node, tr.ttype), [0, 0.0])
                nd[0] += 1
                nd[1] += (te - plan_entry[i]) - a_dl
        node = _node_of_step(net, tr, steps[i])
        last = i == len(steps) - 1
        if is_stop:
            ext = 0.0
            if not last:
                pdw = dm.p_dwell.get(tr.ttype, 0.0) if isinstance(dm.p_dwell, dict) else dm.p_dwell
                mdw = dm.mean_dwell.get(tr.ttype, 1.0) if isinstance(dm.mean_dwell, dict) else dm.mean_dwell
                mdw *= dm.node_factor.get((end_node, tr.ttype), 1.0)
                if tr.ttype != "GV" and rng.random() < pdw:
                    ext = rng.expovariate(1.0 / (mdw * dm.scale))
            dwell = DWELL[tr.ttype] + ext
            app = dm.node_approach.get((end_node, tr.ttype), 0.0) if tr.ttype != "GV" else 0.0
            if app > 0:
                if rng.random() < APPROACH_P:
                    tx += rng.expovariate(APPROACH_P / app)
            elif app < 0 and plan_arr[i] < math.inf:
                tx = max(min(tx, plan_arr[i]), tx + app)
            if node:
                tau = infra.tau(node, eq)
                hold = (tr.hold.get(end_node) if tr.hold else None) or -math.inf
                if last:
                    dur_fn = lambda tp: PLATFORM_TURN[tr.ttype]
                else:
                    dur_fn = lambda tp, hd=hold: max(dwell, hd - tp) + tau
                # trains use their recovery margin rather than arrive early: no node request before plan
                t_req = max(tx, plan_arr[i]) if plan_arr[i] < math.inf else tx
                tp, _ = infra.node_arrive(node, t_req, dur_fn, tau, prefs(idx))
                if tp > t_req:
                    node_wait[node] = node_wait.get(node, 0.0) + tp - t_req
                tx = tp
            dl = max(0.0, tx - plan_arr[i])
            if idx in dep_info and plan_arr[i] < math.inf:
                a_node, dep_dl = dep_info.pop(idx)
                ls = leg_stats.setdefault((a_node, end_node, tr.ttype), [0, 0.0])
                ls[0] += 1
                ls[1] += dl - dep_dl
            if tr.ttype == "FV" or last:
                w = tr.stop_weight()
                delay[tr.ttype] += dl
                pax_delay[tr.ttype] += dl * w
                pax_total[tr.ttype] += w
                n_stops[tr.ttype] += 1
                if dl < 6.0:
                    n_punct[tr.ttype] += 1
                    pax_ontime[tr.ttype] += w
            if tr.ttype != "GV":
                visits[tr.ttype] += 1
                if plan_arr[i] < math.inf:
                    ns = node_stats.setdefault((end_node, tr.ttype), [0, 0, 0.0])
                    ns[0] += 1
                    ns[1] += dl < 6.0
                    ns[2] += dl
                    if not last:
                        arr_info[idx] = (end_node, tx - plan_arr[i])
            if last:
                final_delay[tr.ttype] += dl
                arrived[idx] = (tx, tx - plan_arr[i] if plan_arr[i] < math.inf else last_delay.get(idx, 0.0))
                if idx in swapped_pred:          # vehicle replaced by a reserve: it becomes the reserve
                    sw = swapped_pred.pop(idx)
                    if sw is not None:
                        reserve_units[sw[0]][sw[1]] = tx + fleet.MIN_TURN.get(tr.ttype, 5.0)
                elif idx in waiting:
                    s, tc = waiting.pop(idx)
                    heapq.heappush(pq, (max(tc, ready_after_pred(trains[s], *arrived[idx])), s, 0))
                continue
            nxt = tx + dwell
            if node:
                hold = (tr.hold.get(end_node) if tr.hold else None) or -math.inf
                t_out, _ = infra.node_leave(node, max(nxt, hold), infra.tau(node, eq), prefs(idx))
                nxt = t_out
        elif node:
            t_req = max(tx, plan_arr[i]) if plan_arr[i] < math.inf else tx
            t2, _ = infra.node_pass(node, t_req, infra.tau(node, eq), prefs(idx))
            if t2 > t_req:
                node_wait[node] = node_wait.get(node, 0.0) + t2 - t_req
            nxt = max(tx, t2)
        if i + 1 < len(steps):
            heapq.heappush(pq, (nxt, idx, i + 1))
            if priority:
                announce(idx, i + 1, nxt)
        elif tr.ttype == "GV":
            arrived[idx] = (tx, 0.0)

    pax_sum = sum(pax_total.values())
    pd_all = {k: pax_delay[k] + pax_cancel[k] for k in TYPES}
    return {
        "seed": seed,
        "delay_min": sum(delay.values()),
        # passenger delay incl. cancellations (passengers wait for the next train of the line)
        "pax_delay_min": sum(pd_all.values()),
        "pax_delay_late_min": sum(pax_delay.values()),
        "pax_delay_cancel_min": sum(pax_cancel.values()),
        "pax_total": pax_sum,
        "avg_delay_per_pax": sum(pd_all.values()) / max(pax_sum, 1e-9),
        **{f"delay_{k}": v for k, v in delay.items()},
        **{f"pax_delay_{k}": v for k, v in pd_all.items()},
        **{f"final_delay_{k}": v for k, v in final_delay.items()},
        **{f"punct_{k}": n_punct[k] / max(1, n_stops[k]) for k in TYPES},
        # share of passengers arriving < 6 min late (passenger trains only, cancellations excluded)
        "pax_punct": sum(pax_ontime[k] for k in PAX_TYPES) / max(1e-9, sum(pax_total[k] for k in PAX_TYPES)),
        **{f"pax_punct_{k}": pax_ontime[k] / max(1e-9, pax_total[k]) for k in PAX_TYPES},
        **{f"cancel_share_{k}": sum(canc_visits[k]) / max(1, visits[k]) for k in PAX_TYPES},
        "cancel_visits": {k: list(v) for k, v in canc_visits.items()},
        "visits": dict(visits),
        "n_cancel": {k: list(v) for k, v in n_cancel.items()},
        "starts": {k: list(v) for k, v in starts.items()},
        "turn_stats": turn_stats,
        "n_reserve_used": dict(n_reserve_used),
        "n_incidents": len(incidents),
        "n_reroutes": n_reroutes,
        "n_priority_holds": n_priority_holds,
        "seg_wait": seg_wait,
        "node_wait": node_wait,
        "node_stats": node_stats,
        "node_dwell": node_dwell,
        "leg_stats": leg_stats,
        **({"incidents": incidents} if trace is not None else {}),
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
        path = net.shortest_path(here, target, tr.ttype, tr.vmax, extra_cost=pen, equipped=tr.equipped)
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
