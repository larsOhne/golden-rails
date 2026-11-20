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
    terrain: str = "flat"
    urban_share: float = 0.0
    suburban_share: float = 0.0
    open_share: float = 1.0
    protected_share: float = 0.0
    note: str = ""
    new: bool = False
    nb: int = 0   # block count, fixed by the *base* track layout (upgrades keep it)

    def __post_init__(self):
        if not self.nb:
            lim = MAX_BLOCK_KM_SINGLE if self.tracks <= 1 else MAX_BLOCK_KM_MULTI
            self.nb = max(1, math.ceil(self.length / lim))

    def n_blocks(self) -> int:
        return self.nb

    @property
    def headway(self) -> float:
        return HEADWAY_ETCS if self.etcs else HEADWAY_CONVENTIONAL


@dataclass
class Network:
    nodes: pd.DataFrame
    segments: dict[str, Segment]
    adj: dict[str, list[tuple[str, str, int]]] = field(default_factory=dict)

    def __post_init__(self):
        self.rebuild()

    def rebuild(self):
        self.adj = {n: [] for n in self.nodes.index}
        for s in self.segments.values():
            self.adj[s.a].append((s.b, s.id, 1))
            self.adj[s.b].append((s.a, s.id, -1))

    def copy(self) -> "Network":
        return Network(self.nodes, copy.deepcopy(self.segments))

    # ------------------------------------------------------------------
    def run_minutes(self, seg: Segment, ttype: str, train_vmax: float | None = None) -> float:
        tt = TRAIN_TYPES[ttype]
        v = min(seg.vmax, train_vmax or tt["vmax"], tt["vmax"])
        return seg.length / (v * tt["eff"]) * 60.0

    def shortest_path(self, src: str, dst: str, ttype: str, train_vmax=None,
                      extra_cost=None) -> list[tuple[str, int]]:
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
                if ttype not in seg.types:
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
            elif parts[0] == "etcs":
                self.segments[parts[1]].etcs = True
            else:
                raise ValueError(op)
        self.rebuild()
        return added_track_km


def load_network(data_dir: Path = DATA) -> Network:
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
    return Network(nodes, segs)
