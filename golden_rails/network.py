"""Rail network: nodes, segments, block subdivision and routing."""
from __future__ import annotations

import copy
import os
import heapq
import math
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data"

# Train-type speed and running-time efficiency.
# eff < 1 accounts for acceleration/braking and, for NV, intermediate stops.
TRAIN_TYPES = {
    "FV": {"vmax": 300, "eff": 0.90, "label": "ICE/IC"},
    "NV": {"vmax": 160, "eff": 0.70, "label": "RE/RB"},
    "S": {"vmax": 120, "eff": 0.60, "label": "S-Bahn/tram-train"},
    "GV": {"vmax": 100, "eff": 0.85, "label": "cargo"},
}

HEADWAY_CONVENTIONAL = 3.0   # min, PZB/LZB block signalling
HEADWAY_ETCS = float(os.environ.get("GR_HEADWAY_ETCS", 2.0))  # min, ETCS L2 with short blocks

MAX_BLOCK_KM_SINGLE = 10.0   # passing-loop spacing on single track
MAX_BLOCK_KM_MULTI = 20.0    # crossover spacing on multi-track lines


def channel_layout(tracks: int) -> list[int]:
    """Corridor width -> track channels: +1 forward, -1 backward, 0 bidirectional.

    n tracks give n//2 tracks per direction; an odd track is bidirectional
    (overtaking / counter-peak track). 1 -> [0], 2 -> [+,-], 3 -> [+,-,0],
    4 -> [+,+,-,-], 5 -> [+,+,-,-,0], 6 -> [+,+,+,-,-,-] ...
    """
    if tracks <= 1:
        return [0]
    k = tracks // 2
    lay = [1] * k + [-1] * k
    if tracks % 2:
        lay.append(0)
    return lay


@dataclass
class Segment:
    id: str
    a: str
    b: str
    length: float
    tracks: int
    vmax: float
    types: frozenset
    nv_tph: float
    s_tph: float = 0.0
    etcs: bool = False
    etcs_only: bool = False      # ETCS without lineside signals: only equipped trains may run
    terrain: str = "flat"
    urban_share: float = 0.0
    suburban_share: float = 0.0
    open_share: float = 1.0
    protected_share: float = 0.0
    note: str = ""
    new: bool = False
    nb: int = 0   # block count, fixed by the *base* track layout (upgrades keep it)
    f_fv: float = 1.0    # running-time factor calibrated on GTFS (long-distance)
    f_reg: float = 1.0   # running-time factor calibrated on GTFS (regional / S-Bahn)
    etcs_only_base: bool = False   # ETCS-only today (trains running here today are equipped)

    def __post_init__(self):
        if not self.nb:
            lim = MAX_BLOCK_KM_SINGLE if self.tracks <= 1 else MAX_BLOCK_KM_MULTI
            self.nb = max(1, math.ceil(self.length / lim))

    def n_blocks(self) -> int:
        return self.nb

    @property
    def headway(self) -> float:
        return HEADWAY_ETCS if self.etcs else HEADWAY_CONVENTIONAL

    def hw(self, equipped: bool) -> float:
        """Minimum headway for a following train: the short ETCS headway needs an equipped train;
        unequipped trains run on the lineside signals (mixed traffic)."""
        return HEADWAY_ETCS if (self.etcs and equipped) else HEADWAY_CONVENTIONAL


@dataclass
class NodeCap:
    platforms: int            # main-line platform tracks
    groups: int               # independent route groups in the station throat
    etcs: bool = False        # ETCS in the node (shorter route occupation for equipped trains)


@dataclass
class Network:
    nodes: pd.DataFrame
    segments: dict[str, Segment]
    adj: dict[str, list[tuple[str, str, int]]] = field(default_factory=dict)
    node_cap: dict[str, NodeCap] = field(default_factory=dict)
    fitted: set = field(default_factory=set)          # fleet groups retro-fitted with ETCS
    reserves: dict = field(default_factory=dict)      # (node, group) -> hot reserve train sets

    def __post_init__(self):
        self.rebuild()

    def rebuild(self):
        self.adj = {n: [] for n in self.nodes.index}
        for s in self.segments.values():
            self.adj[s.a].append((s.b, s.id, 1))
            self.adj[s.b].append((s.a, s.id, -1))

    def copy(self) -> "Network":
        return Network(self.nodes, copy.deepcopy(self.segments), node_cap=copy.deepcopy(self.node_cap),
                       fitted=set(self.fitted), reserves=dict(self.reserves))

    # ------------------------------------------------------------------
    def run_minutes(self, seg: Segment, ttype: str, train_vmax: float | None = None) -> float:
        tt = TRAIN_TYPES[ttype]
        v = min(seg.vmax, train_vmax or tt["vmax"], tt["vmax"])
        f = seg.f_fv if ttype == "FV" else seg.f_reg if ttype in ("NV", "S") else max(1.0, seg.f_fv)
        return seg.length / (v * tt["eff"]) * 60.0 * f

    def shortest_path(self, src: str, dst: str, ttype: str, train_vmax=None,
                      extra_cost=None, equipped: bool = True) -> list[tuple[str, int]]:
        """Fastest path as list of (segment_id, direction). extra_cost(seg_id)->min."""
        if src == dst:
            return []
        dist = {src: 0.0}
        prev = {}
        pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == dst:
                break
            if d > dist.get(u, math.inf):
                continue
            for v, sid, direction in self.adj[u]:
                seg = self.segments[sid]
                if ttype not in seg.types or (seg.etcs_only and not equipped):
                    continue
                w = self.run_minutes(seg, ttype, train_vmax) + 1.0  # +1 min node penalty
                if extra_cost:
                    w += extra_cost(sid)
                nd = d + w
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    prev[v] = (u, sid, direction)
                    heapq.heappush(pq, (nd, v))
        if dst not in prev:
            raise ValueError(f"no {ttype} path {src}->{dst}")
        path = []
        cur = dst
        while cur != src:
            u, sid, direction = prev[cur]
            path.append((sid, direction))
            cur = u
        return path[::-1]

    # ------------------------------------------------------------------
    def apply_changes(self, changes: str) -> float:
        """Apply candidate changes; returns added track-km."""
        added_track_km = 0.0
        for op in changes.split(";"):
            parts = op.strip().split(":")
            if parts[0] == "new":
                _, a, b, length, tracks, vmax, types = parts
                sid = f"{a}-{b}-NEW"
                k = 1
                while sid in self.segments:
                    k += 1
                    sid = f"{a}-{b}-NEW{k}"
                # new infrastructure is built with ETCS (constant cost, in capex)
                self.segments[sid] = Segment(
                    sid, a, b, float(length), int(tracks), float(vmax),
                    frozenset(types.split("|")), 0.0, etcs=True, note="candidate", new=True)
                added_track_km += float(length) * int(tracks)
            elif parts[0] == "tracks":
                _, sid, n = parts
                seg = self.segments[sid]
                n = int(n)
                added_track_km += seg.length * max(0, n - seg.tracks)
                seg.tracks = n
            elif parts[0] == "etcs":            # ETCS L2 alongside the lineside signals
                self.segments[parts[1]].etcs = True
            elif parts[0] == "etcs2":           # ETCS only, lineside signals removed
                self.segments[parts[1]].etcs = True
                self.segments[parts[1]].etcs_only = True
            elif parts[0] == "fit":             # ETCS retrofit of a fleet group
                self.fitted.add(parts[1])
            elif parts[0] == "reserve":         # reserve:<node>:<group>:<n>
                _, node, group, n = parts
                self.reserves[(node, group)] = self.reserves.get((node, group), 0) + int(n)
            elif parts[0] == "plat":            # plat:<node>:<k> more platform tracks
                self.node_cap[parts[1]].platforms += int(parts[2])
            elif parts[0] == "throat":          # throat:<node>:<k> more route groups (flyover, approach)
                self.node_cap[parts[1]].groups += int(parts[2])
            elif parts[0] == "netcs":           # ETCS in the node
                self.node_cap[parts[1]].etcs = True
            else:
                raise ValueError(op)
        self.rebuild()
        return added_track_km


def place_name(nodes: pd.DataFrame, nid: str) -> str:
    """Short display name of a node, e.g. 'Hannover Hbf' -> 'Hannover'."""
    n = nodes.loc[nid, "name"]
    for suffix in (" Hbf", " Bad Bf", "-Wilhelmshöhe", " Rbf"):
        n = n.replace(suffix, "")
    return n


def segment_name(net: "Network", sid: str) -> str:
    """Full place names of a corridor, e.g. 'HU-GEL' -> 'Hanau–Gelnhausen'."""
    s = net.segments[sid]
    name = f"{place_name(net.nodes, s.a)}–{place_name(net.nodes, s.b)}"
    if "NBS" in s.note:
        name += " (NBS)"
    return name


def load_network(data_dir: Path = DATA, runtime: bool = True) -> Network:
    nodes = pd.read_csv(data_dir / "nodes.csv").set_index("id")
    segs = {}
    df = pd.read_csv(data_dir / "segments.csv").fillna("")
    for _, r in df.iterrows():
        for n in (r["from"], r["to"]):
            if n not in nodes.index:
                raise ValueError(f"segment {r['id']}: unknown node {n}")
        segs[r["id"]] = Segment(
            r["id"], r["from"], r["to"], float(r["length_km"]), int(r["tracks"]),
            float(r["vmax"]), frozenset(r["types"].split("|")), float(r["nv_tph"]),
            s_tph=float(r["s_tph"]), etcs=bool(int(r["etcs"])), terrain=r["terrain"],
            urban_share=float(r["urban_share"]), suburban_share=float(r["suburban_share"]),
            open_share=float(r["open_share"]), protected_share=float(r["protected_share"]),
            note=r["note"])
        # existing ETCS on new high-speed lines (VDE 8) runs without lineside signals
        seg = segs[r["id"]]
        seg.etcs_only = seg.etcs_only_base = bool(seg.etcs and ("NBS" in seg.note or int(r["etcs"]) == 2))
    rt = data_dir / "segment_runtime.csv"
    if runtime and rt.exists():
        for _, r in pd.read_csv(rt).iterrows():
            if r["segment"] in segs:
                segs[r["segment"]].f_fv = float(r["factor_fv"])
                segs[r["segment"]].f_reg = float(r["factor_reg"])
    return Network(nodes, segs, node_cap=load_node_capacity(data_dir, nodes, segs))


def load_node_capacity(data_dir: Path, nodes, segs) -> dict:
    """Platform tracks per major node (data/nodes_capacity.csv) and route groups in the throat:
    one per approach corridor, two for corridors with four or more tracks."""
    f = data_dir / "nodes_capacity.csv"
    if not f.exists() or os.environ.get("GR_NODES", "1") == "0":
        return {}
    cap = {}
    for r in pd.read_csv(f).itertuples():
        g = sum(1 if s.tracks < 4 else 2 for s in segs.values() if r.node in (s.a, s.b))
        cap[r.node] = NodeCap(int(r.platforms), max(1, g))
    return cap
