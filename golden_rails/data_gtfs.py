"""Real timetable from GTFS (gtfs.de, long-distance 'fv_free' and regional 'rv_free' feeds).

Every rail trip running on the reference day is reduced to the model's nodes:
GTFS stops within a radius of a node are mapped to that node, consecutive calls
at the same node are merged, and legs between two nodes that the model network
cannot reproduce (no path, or a path much longer than the straight line) split
the trip. The result is written to data/timetable_gtfs.csv:

    train_id, category (FV/NV/S), line, agency, stops, dep_min, arr_min

with stops as node ids and departure/arrival minutes after midnight at each node.

    python -m golden_rails.data_gtfs [YYYYMMDD]
"""
from __future__ import annotations

import math
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .network import load_network

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "timetable_gtfs.csv"
REF_DAY = "20261124"   # Tuesday

FV_PREFIX = {"ICE", "IC", "EC", "ECE", "EN", "NJ", "RJ", "RJX", "FLX", "TGV", "ECM", "FEX"}
RADIUS_MAJOR_KM = 1.5
RADIUS_KM = 1.0
DETOUR_LIMIT = 2.0


def _t(s):
    if not isinstance(s, str):
        return math.nan
    h, m, sec = s.split(":")
    return int(h) * 60 + int(m) + int(sec) / 60


def active_services(feed_dir: Path, day: str) -> set[str]:
    cal = pd.read_csv(feed_dir / "calendar.txt", dtype=str)
    wd = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][
        datetime.strptime(day, "%Y%m%d").weekday()]
    on = set(cal[(cal[wd] == "1") & (cal.start_date <= day) & (cal.end_date >= day)].service_id)
    cd = feed_dir / "calendar_dates.txt"
    if cd.exists():
        ex = pd.read_csv(cd, dtype=str)
        ex = ex[ex.date == day]
        on |= set(ex[ex.exception_type == "1"].service_id)
        on -= set(ex[ex.exception_type == "2"].service_id)
    return on


def map_stops(stops: pd.DataFrame, nodes: pd.DataFrame) -> dict[str, str]:
    lat = stops.stop_lat.astype(float).to_numpy()
    lon = stops.stop_lon.astype(float).to_numpy()
    best = {}
    for nid, r in nodes.iterrows():
        d = np.hypot((lat - r.lat) * 111.2, (lon - r.lon) * 111.2 * math.cos(math.radians(r.lat)))
        rad = RADIUS_MAJOR_KM if r.major else RADIUS_KM
        for sid, dist in zip(stops.stop_id[d < rad], d[d < rad]):
            if sid not in best or dist < best[sid][1]:
                best[sid] = (nid, dist)
    return {k: v[0] for k, v in best.items()}


def category(short: str) -> str:
    p = "".join(ch for ch in str(short) if ch.isalpha()).upper()
    if p in FV_PREFIX:
        return "FV"
    if p == "S":
        return "S"
    return "NV"


def build(day: str = REF_DAY) -> pd.DataFrame:
    net = load_network()
    nodes = net.nodes
    ll = nodes[["lat", "lon"]].to_dict("index")

    def crow_km(a, b):
        pa, pb = ll[a], ll[b]
        return math.hypot((pa["lat"] - pb["lat"]) * 111.2,
                          (pa["lon"] - pb["lon"]) * 111.2 * math.cos(math.radians(pa["lat"])))

    path_ok = {}

    def leg_ok(a, b, cat):
        key = (a, b, cat)
        if key not in path_ok:
            ttype = "FV" if cat == "FV" else "NV"
            try:
                p = net.shortest_path(a, b, ttype)
                km = sum(net.segments[s].length for s, _ in p)
                path_ok[key] = km <= DETOUR_LIMIT * crow_km(a, b) + 5
            except ValueError:
                path_ok[key] = False
        return path_ok[key]

    rows = []
    background = []   # calls of trips that touch only one model node (occupy the station, not a corridor)
    stats = {}
    for feed in ("fv", "rv"):
        fd = RAW / f"gtfs_{feed}"
        services = active_services(fd, day)
        routes = pd.read_csv(fd / "routes.txt", dtype=str).set_index("route_id")
        agency = pd.read_csv(fd / "agency.txt", dtype=str).set_index("agency_id").agency_name.to_dict()
        trips = pd.read_csv(fd / "trips.txt", dtype=str)
        trips = trips[trips.service_id.isin(services)]
        stops = pd.read_csv(fd / "stops.txt", dtype=str)
        smap = map_stops(stops, nodes)
        st = pd.read_csv(fd / "stop_times.txt", dtype=str,
                         usecols=["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"])
        st = st[st.trip_id.isin(set(trips.trip_id))]
        st["node"] = st.stop_id.map(smap)
        st["seq"] = st.stop_sequence.astype(int)
        st = st.sort_values(["trip_id", "seq"])
        n_trips, n_kept = len(trips), 0
        tinfo = trips.set_index("trip_id")
        for trip_id, g in st.groupby("trip_id", sort=False):
            g = g[g.node.notna()]
            if g.node.nunique() < 2:
                if len(g):
                    rid = tinfo.loc[trip_id, "route_id"]
                    c1 = category(routes.loc[rid, "route_short_name"] if rid in routes.index else "")
                    if c1 != "S":
                        a, d = _t(g.arrival_time.iloc[0]), _t(g.departure_time.iloc[-1])
                        background.append({"node": g.node.iloc[0], "category": c1,
                                           "arr_min": round(a if a == a else d, 1),
                                           "dep_min": round(d if d == d else a, 1)})
                continue
            rid = tinfo.loc[trip_id, "route_id"]
            short = routes.loc[rid, "route_short_name"] if rid in routes.index else ""
            ag = agency.get(routes.loc[rid, "agency_id"], "") if rid in routes.index else ""
            cat = category(short)
            calls = []  # (node, arr, dep)
            for r in g.itertuples():
                a, d = _t(r.arrival_time), _t(r.departure_time)
                if calls and calls[-1][0] == r.node:
                    calls[-1] = (r.node, calls[-1][1], d)
                else:
                    calls.append((r.node, a, d))
            # split at legs the model network cannot reproduce
            part = [calls[0]]
            pieces = []
            for c in calls[1:]:
                if leg_ok(part[-1][0], c[0], cat):
                    part.append(c)
                else:
                    pieces.append(part)
                    part = [c]
            pieces.append(part)
            for k, p in enumerate(pieces):
                if len(p) < 2:
                    continue
                rows.append({"train_id": f"{feed}-{trip_id}-{k}", "category": cat, "line": short, "agency": ag,
                             "stops": " ".join(c[0] for c in p),
                             "dep_min": " ".join(f"{c[2]:.1f}" for c in p[:-1]),
                             "arr_min": " ".join(f"{c[1]:.1f}" for c in p[1:])})
                n_kept += 1
        stats[feed] = {"trips_on_day": n_trips, "model_trains": n_kept}
    df = pd.DataFrame(rows)
    df.to_csv(OUT, index=False)
    pd.DataFrame(background).to_csv(ROOT / "data" / "node_background.csv", index=False)
    print(stats, df.category.value_counts().to_dict())
    return df


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else REF_DAY)


# ---------------------------------------------------------------------------
# Running-time calibration against the GTFS timetable
# ---------------------------------------------------------------------------
REAL_SUPPLEMENT = 0.07   # recovery margin contained in published running times (Regelzuschlag)
RT_OUT = ROOT / "data" / "segment_runtime.csv"


def runtime_factors(tt_files=None):
    """Per-segment factors so that model minimum running times match GTFS times / (1 + supplement).

    For every leg between two consecutive model stops of a GTFS train, the scheduled
    time (arrival at B minus departure at A) is compared with the model's minimum
    running time along its path. Factors per segment and category group
    (FV, regional = NV+S) are fitted by bounded least squares over all legs.
    """
    from scipy.optimize import lsq_linear
    from .sim import build_trains_gtfs

    net = load_network(runtime=False)
    sids = list(net.segments)
    col = {s: i for i, s in enumerate(sids)}
    pairs = []
    for f in (tt_files or [OUT]):
        tt_ = pd.read_csv(f).drop_duplicates("train_id").set_index("train_id")
        pairs += [(tr, tt_) for tr in build_trains_gtfs(net, tt_file=f)]
    legs = {"FV": [], "REG": []}
    for tr, tt in pairs:
        if tr.ttype == "GV" or tr.id not in tt.index:
            continue
        r = tt.loc[tr.id]
        deps = [float(x) for x in str(r.dep_min).split()]
        arrs = [float(x) for x in str(r.arr_min).split()]
        grp = "FV" if tr.ttype == "FV" else "REG"
        i = 0
        row = np.zeros(len(sids))
        for st in tr.steps:
            row[col[st[0]]] += st[3]
            if st[5]:                       # end of a leg (scheduled stop)
                g = arrs[i] - deps[i]
                if g > 0 and row.sum() > 0:
                    legs[grp].append((row.copy(), g / (1 + REAL_SUPPLEMENT)))
                row[:] = 0
                i += 1
                if i >= len(arrs):
                    break
    out = pd.DataFrame({"segment": sids})
    for grp, L in legs.items():
        A = np.array([l[0] for l in L])
        b = np.array([l[1] for l in L])
        used = A.sum(axis=0) > 0
        # small ridge towards 1 keeps rarely used segments stable
        lam = 0.05 * np.median(A[:, used].sum(axis=0))
        A2 = np.vstack([A[:, used], lam * np.eye(used.sum())])
        b2 = np.concatenate([b, lam * np.ones(used.sum())])
        sol = lsq_linear(A2, b2, bounds=(0.8, 3.0))
        f = np.ones(len(sids))
        f[used] = sol.x
        out[f"factor_{grp.lower()}"] = np.round(f, 3)
        out[f"legs_{grp.lower()}"] = (A > 0).sum(axis=0)
        pred = A @ f
        print(f"{grp}: {len(L)} legs; median |error| after fit {np.median(np.abs(pred - b) / b) * 100:.1f}% "
              f"(before {np.median(np.abs(A.sum(axis=1) - b) / b) * 100:.1f}%)")
    out.to_csv(RT_OUT, index=False)
    return out
