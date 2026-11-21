"""Derive corridor attributes from OpenStreetMap rail data.

For every model segment (corridor between two nodes):
  1. both nodes are snapped to the OSM rail network near the station,
  2. the shortest OSM path between them is found on main-line track
     (high-speed ways preferred for NBS segments, avoided otherwise),
  3. along the path we measure length, maxspeed (km-weighted median), share
     electrified, share with ETCS / LZB, and the number of parallel main-line
     tracks (German OSM maps every track as its own way, so tracks are counted
     at sample points away from stations, median over the corridor).

Writes data/segments_osm.csv. With ``apply=True`` the derived attributes are
written into data/segments.csv (curated values kept where OSM is not reliable).

    python -m golden_rails.osm_network [--apply]
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from .network import DATA, load_network

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "osm"
OUT = DATA / "segments_osm.csv"
LAT0 = 51.0
KX, KY = 111.32 * math.cos(math.radians(LAT0)), 110.57   # km per degree


def xy(lat, lon):
    return np.asarray(lon) * KX, np.asarray(lat) * KY


def parse_speed(v):
    if not isinstance(v, str):
        return np.nan
    v = v.split(";")[0].strip().lower()
    try:
        if "mph" in v:
            return float(v.replace("mph", "")) * 1.609
        return float(v)
    except ValueError:
        return np.nan


def load_rail():
    df = pd.read_parquet(RAW / "rail_de.parquet")
    main = df.service.isna() & (df.usage.isin(["main", "branch"]) | df.usage.isna())
    main &= df.gauge.fillna("1435").astype(str).str.contains("1435")
    df = df[main].reset_index(drop=True)
    df["speed"] = df.maxspeed.map(parse_speed)
    df["is_hs"] = df.highspeed.eq("yes")
    df["is_el"] = df.electrified.fillna("no").ne("no")
    etcs = df["railway:etcs"].fillna("no").astype(str)
    df["is_etcs"] = ~etcs.isin(["no", "0", ""])
    df["is_lzb"] = df["railway:lzb"].fillna("no").eq("yes")
    return df


class RailGraph:
    def __init__(self, df: pd.DataFrame):
        self.df = df
        # junctions: way endpoints and nodes shared by several ways
        counts: dict[int, int] = {}
        for ids in df.node_ids:
            for n in (ids[0], ids[-1]):
                counts[n] = counts.get(n, 0) + 2
            for n in ids[1:-1]:
                counts[n] = counts.get(n, 0) + 1
        junction = {n for n, c in counts.items() if c >= 2}
        idx: dict[int, int] = {}
        coords = []
        rows, cols, w, eway = [], [], [], []
        self.edge_info = []
        for wi, (ids, lats, lons) in enumerate(zip(df.node_ids, df.lats, df.lons)):
            x, y = xy(lats, lons)
            seglen = np.hypot(np.diff(x), np.diff(y))
            cum = np.concatenate([[0], np.cumsum(seglen)])
            last = 0
            for k in range(1, len(ids)):
                if ids[k] in junction or k == len(ids) - 1:
                    a, b = ids[last], ids[k]
                    for n, j in ((a, last), (b, k)):
                        if n not in idx:
                            idx[n] = len(coords)
                            coords.append((x[j], y[j]))
                    L = cum[k] - cum[last]
                    rows.append(idx[a]); cols.append(idx[b]); w.append(max(L, 1e-4)); eway.append(wi)
                    last = k
        self.node_index = idx
        self.coords = np.array(coords)
        self.tree = cKDTree(self.coords)
        self.e_from, self.e_to = np.array(rows), np.array(cols)
        self.e_len, self.e_way = np.array(w), np.array(eway)
        self.n = len(coords)
        # densified points of all main-line ways for track counting
        px, py, pw = [], [], []
        for wi, (lats, lons) in enumerate(zip(df.lats, df.lons)):
            x, y = xy(lats, lons)
            for k in range(len(x) - 1):
                L = math.hypot(x[k + 1] - x[k], y[k + 1] - y[k])
                m = max(1, int(L / 0.02))
                t = np.linspace(0, 1, m, endpoint=False)
                px.append(x[k] + (x[k + 1] - x[k]) * t)
                py.append(y[k] + (y[k + 1] - y[k]) * t)
                pw.append(np.full(m, wi))
        self.pts = np.c_[np.concatenate(px), np.concatenate(py)]
        self.pts_way = np.concatenate(pw)
        self.pts_tree = cKDTree(self.pts)

    def _graph(self, prefer_hs: bool):
        if not hasattr(self, "_g"):
            self._g = {}
        if prefer_hs not in self._g:
            hs = self.df.is_hs.to_numpy()[self.e_way]
            wgt = self.e_len * np.where(hs == prefer_hs, 1.0, 1.6)
            self._g[prefer_hs] = coo_matrix(
                (np.r_[wgt, wgt], (np.r_[self.e_from, self.e_to], np.r_[self.e_to, self.e_from])),
                shape=(self.n, self.n)).tocsr()
        return self._g[prefer_hs]

    def path(self, a_xy, b_xy, prefer_hs: bool, snap_km=1.5):
        sa = self.tree.query_ball_point(a_xy, snap_km)
        sb = self.tree.query_ball_point(b_xy, snap_km)
        if not sa or not sb:
            return None
        g = self._graph(prefer_hs)
        # multi-source Dijkstra from all snap candidates of A (with distance-to-station offset)
        dist, pred, src = dijkstra(g, indices=sa, return_predecessors=True, min_only=True)
        off_b = np.hypot(*(self.coords[sb] - b_xy).T)
        best = sb[int(np.argmin(dist[sb] + off_b))]
        if not np.isfinite(dist[best]):
            return None
        nodes = [best]
        while pred[nodes[-1]] >= 0:
            nodes.append(pred[nodes[-1]])
        return nodes[::-1], g


def analyse(prefer_cache=True):
    net = load_network()
    df = load_rail()
    G = RailGraph(df)
    # edge lookup (u,v) -> edge index (shortest)
    lookup: dict[tuple[int, int], int] = {}
    for i, (u, v, L) in enumerate(zip(G.e_from, G.e_to, G.e_len)):
        for k in ((u, v), (v, u)):
            if k not in lookup or L < G.e_len[lookup[k]]:
                lookup[k] = i
    rows = []
    for s in net.segments.values():
        a = xy(net.nodes.loc[s.a, "lat"], net.nodes.loc[s.a, "lon"])
        b = xy(net.nodes.loc[s.b, "lat"], net.nodes.loc[s.b, "lon"])
        res = G.path(np.array(a), np.array(b), prefer_hs="NBS" in s.note)
        if res is None:
            rows.append({"segment": s.id, "found": False})
            continue
        nodes, _ = res
        eidx = [lookup[(u, v)] for u, v in zip(nodes[:-1], nodes[1:]) if (u, v) in lookup]
        L = G.e_len[eidx]
        ways = G.e_way[eidx]
        sub = df.iloc[ways]
        length = float(L.sum())
        sp = sub.speed.to_numpy()
        ok = np.isfinite(sp)
        if ok.any():
            o = np.argsort(sp[ok])
            cw = np.cumsum(L[ok][o]) / L[ok].sum()
            vmed = float(sp[ok][o][np.searchsorted(cw, 0.5)])
        else:
            vmed = np.nan
        share = lambda col: float((L * sub[col].to_numpy()).sum() / max(length, 1e-9))
        # parallel tracks: sample points along the path, away from the corridor ends
        pts = G.coords[nodes]
        seg_l = np.hypot(*np.diff(pts, axis=0).T)
        cum = np.r_[0, np.cumsum(seg_l)]
        counts = []
        if cum[-1] > 4:
            for d in np.arange(2.5, cum[-1] - 2.5, 1.7):
                k = min(np.searchsorted(cum, d) - 1, len(pts) - 2)
                t = (d - cum[k]) / max(seg_l[k], 1e-9)
                p = pts[k] + (pts[k + 1] - pts[k]) * t
                dirv = (pts[k + 1] - pts[k]) / max(seg_l[k], 1e-9)
                near = G.pts_tree.query_ball_point(p, 0.03)
                cand = set()
                for j in near:
                    wj = G.pts_way[j]
                    cand.add(wj)
                # only roughly parallel ways
                par = 0
                for wj in cand:
                    la, lo = df.lats.iat[wj], df.lons.iat[wj]
                    wx, wy = xy([la[0], la[-1]], [lo[0], lo[-1]])
                    v = np.array([wx[1] - wx[0], wy[1] - wy[0]])
                    if np.linalg.norm(v) < 1e-6:
                        continue
                    if abs(np.dot(v / np.linalg.norm(v), dirv)) > 0.8:
                        par += 1
                tag = pd.to_numeric(df.tracks.iloc[list(cand)], errors="coerce").max() if cand else np.nan
                counts.append(max(par, tag if np.isfinite(tag) else 0))
        tracks = int(np.median(counts)) if counts else np.nan
        rows.append({"segment": s.id, "found": True, "osm_length_km": round(length, 1),
                     "osm_vmax": vmed, "osm_tracks": tracks,
                     "osm_tracks_p25": float(np.percentile(counts, 25)) if counts else np.nan,
                     "share_electrified": round(share("is_el"), 3), "share_etcs": round(share("is_etcs"), 3),
                     "share_lzb": round(share("is_lzb"), 3), "share_highspeed": round(share("is_hs"), 3),
                     "curated_length_km": s.length, "curated_tracks": s.tracks, "curated_vmax": s.vmax,
                     "n_track_samples": len(counts)})
    out = pd.DataFrame(rows)
    out.to_csv(OUT, index=False)
    return out


def apply(osm: pd.DataFrame, path=DATA / "segments.csv"):
    """Write OSM-derived attributes into segments.csv where they are plausible."""
    seg = pd.read_csv(path)
    o = osm.set_index("segment")
    changed = []
    for i, r in seg.iterrows():
        if r["id"] not in o.index or not o.loc[r["id"], "found"]:
            continue
        x = o.loc[r["id"]]
        # length: accept when the OSM path is not an obvious detour
        if 0.75 * r["length_km"] <= x.osm_length_km <= 1.35 * r["length_km"]:
            seg.at[i, "length_km"] = round(float(x.osm_length_km))
        if np.isfinite(x.osm_tracks) and 1 <= x.osm_tracks <= 6 and x.n_track_samples >= 3:
            if int(x.osm_tracks) != r["tracks"]:
                changed.append((r["id"], "tracks", r["tracks"], int(x.osm_tracks)))
            seg.at[i, "tracks"] = int(x.osm_tracks)
        if np.isfinite(x.osm_vmax) and 60 <= x.osm_vmax <= 300:
            seg.at[i, "vmax"] = int(x.osm_vmax)
        seg.at[i, "etcs"] = int(x.share_etcs >= 0.5)
    seg.to_csv(path, index=False)
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    osm = analyse()
    pd.set_option("display.width", 220)
    print(osm.drop(columns=["found"]).round(2).to_string(index=False))
    if a.apply:
        ch = apply(osm)
        print("track changes:", ch)


if __name__ == "__main__":
    main()
