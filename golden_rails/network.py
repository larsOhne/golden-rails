"""Rail network: nodes, segments, block subdivision and routing."""
from __future__ import annotations

import copy
import heapq
import math
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data"

# Train-type speed and running-time efficiency.
# eff < 1 accounts for acceleration/braking and, for NV, intermediate stops.
TRAIN_TYPES = {
    "FV": {"vmax": 300, "eff": 0.90},
    "NV": {"vmax": 160, "eff": 0.70},
    "GV": {"vmax": 100, "eff": 0.85},
}

MAX_BLOCK_KM_SINGLE = 10.0   # passing-loop spacing on single track
MAX_BLOCK_KM_MULTI = 20.0    # crossover spacing on multi-track lines


def channel_layout(tracks: int) -> list[int]:
    """Direction per track channel: +1 forward, -1 backward, 0 bidirectional."""
    if tracks <= 1:
        return [0]
    if tracks == 2:
        return [1, -1]
    if tracks == 3:
        return [1, -1, 0]
    return [1, 1, -1, -1]


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
    upgrade_meur_per_km: float
    note: str = ""
    new: bool = False
    nb: int = 0   # block count, fixed by the *base* track layout (upgrades keep it)

    def __post_init__(self):
        if not self.nb:
            lim = MAX_BLOCK_KM_SINGLE if self.tracks <= 1 else MAX_BLOCK_KM_MULTI
            self.nb = max(1, math.ceil(self.length / lim))

    def n_blocks(self) -> int:
        return self.nb


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
                self.segments[sid] = Segment(
                    sid, a, b, float(length), int(tracks), float(vmax),
                    frozenset(types.split("|")), 0.0, 0.0, "candidate", new=True)
                added_track_km += float(length) * int(tracks)
            elif parts[0] == "tracks":
                _, sid, n = parts
                seg = self.segments[sid]
                n = int(n)
                added_track_km += seg.length * max(0, n - seg.tracks)
                seg.tracks = n
            else:
                raise ValueError(op)
        self.rebuild()
        return added_track_km


def load_network(data_dir: Path = DATA) -> Network:
    nodes = pd.read_csv(data_dir / "nodes.csv").set_index("id")
    segs = {}
    df = pd.read_csv(data_dir / "segments.csv").fillna("")
    for r in df.itertuples():
        for n in (r._2, r.to):
            if n not in nodes.index:
                raise ValueError(f"segment {r.id}: unknown node {n}")
        segs[r.id] = Segment(r.id, r._2, r.to, float(r.length_km), int(r.tracks),
                             float(r.vmax), frozenset(r.types.split("|")),
                             float(r.nv_tph), float(r.upgrade_meur_per_km), r.note)
    return Network(nodes, segs)
