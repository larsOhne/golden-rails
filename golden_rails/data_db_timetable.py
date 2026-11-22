"""Timetables from the planned times in the DB delay data, one per month.

For each month a representative weekday (Tuesday to Thursday with the median number of runs) is
taken; every run is reduced to the model nodes exactly like the GTFS timetable (consecutive calls at
the same node merged, legs the model network cannot reproduce split the run). This gives a timetable
from the same source and period as the observed delays, including the detours and closures that
were actually planned in that month.

Outputs: data/timetables/tt_YYYY-MM.csv (same columns as data/timetable_gtfs.csv) and
data/timetables/bg_YYYY-MM.csv (calls of runs that touch a single node).

    python -m golden_rails.data_db_timetable 2026-05 [2026-06 ...]
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from .data_delays import category, node_stations
from .network import load_network

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "delays"
OUT = ROOT / "data" / "timetables"
DETOUR_LIMIT = 2.0


def _crow(ll, a, b):
    pa, pb = ll[a], ll[b]
    return math.hypot((pa["lat"] - pb["lat"]) * 111.2,
                      (pa["lon"] - pb["lon"]) * 111.2 * math.cos(math.radians(pa["lat"])))


def build(month: str):
    OUT.mkdir(parents=True, exist_ok=True)
    cols = ["station_name", "train_type", "train_number", "line_number", "train_line_station_num",
            "arrival_planned_time", "departure_planned_time", "is_replacement_train", "id"]
    df = pq.read_table(RAW / f"data-{month}.parquet", columns=cols).to_pandas()
    df = df[~df.is_replacement_train]
    df["cat"] = category(df.train_type)
    df = df[df.cat != "DROP"]
    df["trip"] = df["id"].str.rsplit("-", n=1).str[0]
    t = df.departure_planned_time.fillna(df.arrival_planned_time)
    df["t"] = t
    start = df.groupby("trip").t.min()
    day = start.dt.normalize()
    counts = day.value_counts()
    wd = counts[[d.weekday() in (1, 2, 3) for d in counts.index]]
    ref = (wd - wd.median()).abs().sort_values().index[0]
    trips = set(start[day == ref].index)
    d = df[df.trip.isin(trips)].copy()
    net = load_network()
    mapping = node_stations(d.station_name)
    inv = {v: k for k, v in mapping.items()}
    d["node"] = d.station_name.map(inv)
    d = d[d.node.notna()].sort_values(["trip", "train_line_station_num"])
    mins = lambda x: (x - ref).dt.total_seconds() / 60.0
    d["arr"] = mins(d.arrival_planned_time)
    d["dep"] = mins(d.departure_planned_time)
    d = d[d.arr.notna() | d.dep.notna()]
    ll = net.nodes[["lat", "lon"]].to_dict("index")
    path_ok = {}

    def leg_ok(a, b, cat):
        k = (a, b, cat)
        if k not in path_ok:
            try:
                p = net.shortest_path(a, b, "FV" if cat == "FV" else "NV")
                km = sum(net.segments[s].length for s, _ in p)
                path_ok[k] = km <= DETOUR_LIMIT * _crow(ll, a, b) + 5
            except ValueError:
                path_ok[k] = False
        return path_ok[k]

    rows, bg = [], []
    for trip, g in d.groupby("trip", sort=False):
        cat = g.cat.iloc[0]
        tt = str(g.train_type.iloc[0])
        ln = str(g.line_number.iloc[0]) if pd.notna(g.line_number.iloc[0]) else ""
        line = tt if cat == "FV" else (ln or tt)
        calls = []
        for r in g.itertuples():
            a = r.arr if r.arr == r.arr else r.dep
            dp = r.dep if r.dep == r.dep else r.arr
            if calls and calls[-1][0] == r.node:
                calls[-1] = (r.node, calls[-1][1], dp)
            else:
                calls.append((r.node, a, dp))
        if len(calls) < 2:
            if cat != "S" and calls:
                bg.append({"node": calls[0][0], "category": cat, "arr_min": round(calls[0][1], 1),
                           "dep_min": round(calls[0][2], 1)})
            continue
        part, pieces = [calls[0]], []
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
            rows.append({"train_id": f"db-{trip}-{k}", "category": cat, "line": line, "agency": tt,
                         "stops": " ".join(c[0] for c in p),
                         "dep_min": " ".join(f"{c[2]:.1f}" for c in p[:-1]),
                         "arr_min": " ".join(f"{c[1]:.1f}" for c in p[1:])})
    out = pd.DataFrame(rows)
    out.to_csv(OUT / f"tt_{month}.csv", index=False)
    pd.DataFrame(bg).to_csv(OUT / f"bg_{month}.csv", index=False)
    print(month, ref.date(), out.category.value_counts().to_dict(), "background", len(bg), flush=True)
    return out


if __name__ == "__main__":
    for m in sys.argv[1:]:
        build(m)
