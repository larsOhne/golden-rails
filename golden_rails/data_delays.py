"""Observed delays from the DB Timetables API (dataset piebro/deutsche-bahn-data, CC BY 4.0).

From the monthly processed stop data (one row per train stop with planned and
actual times) this computes, for passenger trains:

* punctuality per category (share of arrivals < 6 min late, cancellations excluded),
* the distribution of departure delays at the train's first station (origin delay),
* punctuality and mean arrival delay at each model node,
* delay change between consecutive model nodes per corridor (where delays grow).

Outputs (small CSVs, committed): data/observed/*.csv

    python -m golden_rails.data_delays data/raw/delays/data-2026-10.parquet
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from .network import load_network, place_name

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "observed"

FV_TYPES = {"ICE", "IC", "EC", "ECE", "EN", "NJ", "RJ", "RJX", "TGV", "FLX", "EST", "D", "IR", "ICD"}
S_TYPES = {"S", "AVG"}
DROP_TYPES = {"Bus", "BUS", "STR"}

# DB station names for the model nodes where the generic rule below does not hit
NODE_STATION = {
    "MS": "Münster (Westf) Hbf", "HAM": "Hamm (Westf) Hbf", "VER": "Verden (Aller)", "ROT": "Rotenburg (Wümme)",
    "MI": "Minden (Westf)", "SIE": "Siegburg/Bonn", "FFLF": "Frankfurt am Main Flughafen Fernbahnhof",
    "F": "Frankfurt (Main) Hbf", "FR": "Freiburg (Breisgau) Hbf", "BAS": "Basel Bad Bf", "HAL": "Halle (Saale) Hbf",
    "WB": "Lutherstadt Wittenberg Hbf", "KS": "Kassel-Wilhelmshöhe", "SPA": "Berlin-Spandau", "WI": "Mainz-Kastel",
    "EIS": "Eisenach", "OB": "Oberhausen Hbf", "HL": "Lübeck Hbf", "SDL": "Stendal Hbf", "MD": "Magdeburg Hbf",
    "WOB": "Wolfsburg Hbf", "BS": "Braunschweig Hbf", "IN": "Ingolstadt Hbf", "PA": "Passau Hbf",
    "HI": "Hildesheim Hbf", "BI": "Bielefeld Hbf", "BO": "Bochum Hbf", "HD": "Heidelberg Hbf",
    "KL": "Kaiserslautern Hbf", "SB": "Saarbrücken Hbf", "WUP": "Wuppertal Hbf", "DA": "Darmstadt Hbf",
    "HU": "Hanau Hbf", "EF": "Erfurt Hbf", "R": "Regensburg Hbf", "U": "Ulm Hbf", "A": "Augsburg Hbf",
    "CE": "Celle Pbf", "B": "Berlin Hauptbahnhof",
}


def category(tt: pd.Series) -> pd.Series:
    out = pd.Series("NV", index=tt.index)
    out[tt.isin(FV_TYPES)] = "FV"
    out[tt.isin(S_TYPES)] = "S"
    out[tt.isin(DROP_TYPES)] = "DROP"
    return out


def node_stations(names: pd.Series) -> dict[str, str]:
    """Model node -> DB station name (most frequent matching name)."""
    net = load_network()
    counts = names.value_counts()
    mapping = {}
    for nid in net.nodes.index:
        if nid in NODE_STATION and NODE_STATION[nid] in counts.index:
            mapping[nid] = NODE_STATION[nid]
            continue
        pn = place_name(net.nodes, nid)
        cands = [f"{pn} Hbf", pn, net.nodes.loc[nid, "name"]]
        for c in cands:
            if c in counts.index:
                mapping[nid] = c
                break
    return mapping


def load(path: Path) -> pd.DataFrame:
    cols = ["station_name", "train_type", "train_number", "train_line_ride_id", "train_line_station_num",
            "arrival_planned_time", "arrival_change_time", "departure_planned_time", "departure_change_time",
            "arrival_is_canceled", "departure_is_canceled", "is_replacement_train", "is_additional_stop", "id", "line_number"]
    df = pq.read_table(path, columns=cols).to_pandas()
    # one run of a train on one day: ride id + start timestamp (the ride id alone repeats every day)
    df["trip"] = df["id"].str.rsplit("-", n=1).str[0]
    df = df.drop(columns=["id"])
    df["cat"] = category(df.train_type)
    df = df[(df.cat != "DROP") & ~df.is_replacement_train & ~df.is_additional_stop]
    arr = (df.arrival_change_time.fillna(df.arrival_planned_time) - df.arrival_planned_time)
    dep = (df.departure_change_time.fillna(df.departure_planned_time) - df.departure_planned_time)
    df["arr_delay"] = arr.dt.total_seconds() / 60
    df["dep_delay"] = dep.dt.total_seconds() / 60
    df.loc[df.arrival_is_canceled, "arr_delay"] = np.nan
    df.loc[df.departure_is_canceled, "dep_delay"] = np.nan
    return df


def analyse(path: Path, out=OUT):
    out.mkdir(parents=True, exist_ok=True)
    df = load(path)
    month = re.search(r"(\d{4}-\d{2})", path.name).group(1)

    # 1. punctuality per category (DB definition: arrivals < 6 min, cancellations excluded)
    a = df[df.arrival_planned_time.notna()]
    punct = a.groupby("cat").agg(
        stops=("arr_delay", "size"),
        cancelled_share=("arr_delay", lambda x: x.isna().mean()),
        punct_lt6=("arr_delay", lambda x: (x.dropna() < 6).mean()),
        punct_lt16=("arr_delay", lambda x: (x.dropna() < 16).mean()),
        mean_delay=("arr_delay", "mean"),
    ).reset_index()
    punct.insert(0, "month", month)
    punct.to_csv(out / "punctuality_by_category.csv", index=False)

    # 2. origin departure delays (first station of each ride)
    first = df.loc[df.groupby("trip").train_line_station_num.idxmin()]
    first = first[first.departure_planned_time.notna() & first.dep_delay.notna()]
    rows = []
    for cat, g in first.groupby("cat"):
        d = g.dep_delay.clip(lower=0)
        late = d[d >= 1]
        rows.append({"cat": cat, "trains": len(d), "p_delayed_ge1": (d >= 1).mean(),
                     "mean_if_delayed": late.mean(), "p_ge6": (d >= 6).mean(), "p_ge16": (d >= 16).mean(),
                     **{f"q{q}": d.quantile(q / 100) for q in (50, 75, 90, 95, 99)}})
    origin = pd.DataFrame(rows)
    origin.to_csv(out / "origin_delay.csv", index=False)
    hist = (first.assign(d=first.dep_delay.clip(lower=0, upper=120).round())
            .groupby(["cat", "d"]).size().rename("n").reset_index())
    hist.to_csv(out / "origin_delay_hist.csv", index=False)

    # 3. per model node
    mapping = node_stations(df.station_name)
    inv = {v: k for k, v in mapping.items()}
    a = a.assign(node=a.station_name.map(inv))
    an = a[a.node.notna()]
    per_node = an.groupby(["node", "cat"]).agg(
        stops=("arr_delay", "size"), punct_lt6=("arr_delay", lambda x: (x.dropna() < 6).mean()),
        mean_delay=("arr_delay", "mean")).reset_index()
    per_node["station"] = per_node.node.map(mapping)
    per_node.to_csv(out / "punctuality_by_node.csv", index=False)

    # 4. delay change between consecutive model nodes within the same ride
    s = df.assign(node=df.station_name.map(inv))
    s = s[s.node.notna()].sort_values(["trip", "train_line_station_num"])
    s["next_node"] = s.groupby("trip").node.shift(-1)
    s["next_arr_delay"] = s.groupby("trip").arr_delay.shift(-1)
    legs = s[s.next_node.notna() & (s.next_node != s.node) & s.dep_delay.notna() & s.next_arr_delay.notna()]
    legs = legs.assign(change=legs.next_arr_delay - legs.dep_delay)
    corr = legs.groupby(["node", "next_node", "cat"]).agg(
        trains=("change", "size"), mean_change=("change", "mean"),
        p_gain_ge3=("change", lambda x: (x >= 3).mean()),
        mean_dep_delay=("dep_delay", "mean")).reset_index()
    corr.to_csv(out / "delay_change_by_leg.csv", index=False)

    # 5. delay when a train enters the model: departure delay at its first model node
    entry = s[s.dep_delay.notna()].groupby("trip").head(1)
    e = entry.assign(d=entry.dep_delay.clip(lower=0))
    by_node = e.groupby(["node", "cat"]).agg(
        trains=("d", "size"), p_delayed_ge1=("d", lambda x: (x >= 1).mean()),
        mean_if_delayed=("d", lambda x: x[x >= 1].mean())).reset_index()
    by_node.to_csv(out / "entry_delay_by_node.csv", index=False)
    (e.assign(d=e.d.clip(upper=120).round()).groupby(["cat", "d"]).size().rename("n").reset_index()
     .to_csv(out / "entry_delay_hist.csv", index=False))

    # 5b. delay gained while standing in a model node (departure delay - arrival delay)
    st = s[s.dep_delay.notna() & s.arr_delay.notna()]
    st.assign(dwell_gain=st.dep_delay - st.arr_delay).groupby(["node", "cat"]).agg(
        stops=("dwell_gain", "size"), mean_dwell_gain=("dwell_gain", "mean")).reset_index().to_csv(
        out / "node_dwell_gain.csv", index=False)

    # 6. cancellations: share of cancelled stops, split into full (all stops of the run), tail (from a
    #    station on to the end: typical for a failure en route or a short turn) and other
    cancellations(df).to_csv(out / "cancellations.csv", index=False)

    # 7. turnarounds at model nodes: arrival delay of a terminating run vs departure delay of the next
    #    run of the same line starting there (vehicle circulation, matched first-in-first-out)
    turnaround(df, inv).to_csv(out / "turnaround.csv", index=False)

    meta = {"source": "piebro/deutsche-bahn-data (DB Timetables API, CC BY 4.0)", "file": path.name,
            "month": month, "rows_used": int(len(df)), "node_station_map": mapping}
    (out / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    return punct, origin, per_node, corr


def cancellations(df: pd.DataFrame) -> pd.DataFrame:
    s = df[["trip", "cat", "train_line_station_num", "arrival_is_canceled", "departure_is_canceled",
            "arrival_planned_time", "departure_planned_time"]].copy()
    s["canc"] = s.arrival_is_canceled.fillna(False) | s.departure_is_canceled.fillna(False)
    s = s.sort_values(["trip", "train_line_station_num"])
    g = s.groupby("trip")
    s["n"] = g.canc.transform("size")
    s["n_canc"] = g.canc.transform("sum")
    # tail: every stop from the first cancelled one to the end is cancelled
    s["rev_all"] = s.iloc[::-1].groupby("trip").canc.cummin().iloc[::-1].astype(bool)
    first_c = s[s.canc].groupby("trip").train_line_station_num.min()
    s["first_c"] = s.trip.map(first_c)
    tail_ok = s[s.train_line_station_num >= s.first_c].groupby("trip").canc.all()
    s["kind"] = "none"
    s.loc[s.n_canc > 0, "kind"] = "other"
    s.loc[s.trip.map(tail_ok).fillna(False).astype(bool) & (s.n_canc > 0), "kind"] = "tail"
    s.loc[(s.n_canc == s.n) & (s.n > 0), "kind"] = "full"
    trips = s.groupby("trip").agg(cat=("cat", "first"), n=("n", "first"), n_canc=("n_canc", "first"),
                                  kind=("kind", "first"))
    rows = []
    for cat, t in trips.groupby("cat"):
        stops = t.n.sum()
        rows.append({"cat": cat, "trips": len(t), "stops": int(stops),
                     "cancelled_stop_share": t.n_canc.sum() / stops,
                     "share_full": t.loc[t.kind == "full", "n_canc"].sum() / max(1, t.n_canc.sum()),
                     "share_tail": t.loc[t.kind == "tail", "n_canc"].sum() / max(1, t.n_canc.sum()),
                     "share_other": t.loc[t.kind == "other", "n_canc"].sum() / max(1, t.n_canc.sum()),
                     "trips_full": (t.kind == "full").mean(), "trips_tail": (t.kind == "tail").mean(),
                     "trips_other": (t.kind == "other").mean()})
    return pd.DataFrame(rows)


def turnaround(df: pd.DataFrame, inv: dict, max_wait=120.0, min_turn=3.0) -> pd.DataFrame:
    """Pairs (terminating run, next run of the same line starting at that station) at model nodes."""
    allst = df.sort_values(["trip", "train_line_station_num"])
    last = allst.groupby("trip").tail(1)
    first = allst.groupby("trip").head(1)
    last = last[last.station_name.isin(inv) & last.arrival_planned_time.notna() & last.arr_delay.notna()]
    first = first[first.station_name.isin(inv) & first.departure_planned_time.notna() & first.dep_delay.notna()]
    rows = []
    key = lambda d: d.station_name + "|" + d.train_type.astype(str) + "|" + d.line_number.astype(str)
    last = last.assign(k=key(last))
    first = first.assign(k=key(first))
    fg = {k: g.sort_values("departure_planned_time") for k, g in first.groupby("k")}
    for k, arr in last.groupby("k"):
        dep = fg.get(k)
        if dep is None:
            continue
        dts = dep.departure_planned_time.to_numpy()
        used = np.zeros(len(dep), bool)
        for r in arr.sort_values("arrival_planned_time").itertuples():
            lo = r.arrival_planned_time + pd.Timedelta(minutes=min_turn)
            j = np.searchsorted(dts, np.datetime64(lo))
            while j < len(dts) and used[j]:
                j += 1
            if j >= len(dts):
                continue
            gap = (dts[j] - np.datetime64(r.arrival_planned_time)) / np.timedelta64(1, "m")
            if gap > max_wait:
                continue
            used[j] = True
            rows.append({"node": inv[r.station_name], "cat": r.cat, "turn_min": gap,
                         "arr_delay": r.arr_delay, "dep_delay": float(dep.dep_delay.iloc[j])})
    t = pd.DataFrame(rows)
    t["arr_bin"] = pd.cut(t.arr_delay.clip(lower=0), [-0.1, 5, 10, 20, 40, 1e9],
                          labels=["0-5", "5-10", "10-20", "20-40", ">40"])
    return t.groupby(["cat", "arr_bin"], observed=True).agg(
        n=("dep_delay", "size"), mean_turn=("turn_min", "mean"), mean_arr=("arr_delay", "mean"),
        mean_dep=("dep_delay", "mean"), p_dep_ge3=("dep_delay", lambda x: (x >= 3).mean()),
        p_dep_ge6=("dep_delay", lambda x: (x >= 6).mean())).reset_index()


def main():
    path =Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data/raw/delays/data-2026-10.parquet"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else OUT
    punct, origin, per_node, corr = analyse(path, out)
    pd.set_option("display.width", 200)
    print(punct.round(3).to_string(index=False))
    print(origin.round(2).to_string(index=False))


if __name__ == "__main__":
    main()
