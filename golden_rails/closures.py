"""Find full closures in the observed year and use them as backtests of the capacity response.

A leg between two consecutive model nodes counts as closed in a month when the number of trains
observed on it falls below ``CLOSED_SHARE`` of its median month (legs with a median of at least
``MIN_TRAINS`` trains per month; both directions and all passenger categories pooled). Closed legs
are mapped onto model corridors; corridors closed in the same months form one closure event.

For every event the model simulates the current timetable with those corridors removed (long-distance
and freight trains detour, regional trains on the closed corridors are dropped) and compares the
change of punctuality per station with the observed change between the closure months and the
other months of the year (difference in differences against the national change of each category).

    python -m golden_rails.closures            # detect events -> results/closure_events.csv
    python -m golden_rails.closures --backtest # simulate them -> results/backtest_events.{csv,json}
"""
from __future__ import annotations

import argparse
import json
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

from . import validate as V
from .network import load_network, segment_name
from .observed_agg import YEAR, leg_counts, month_dir
from .sim import DisruptionModel, build_trains, plan_timetable, simulate_day

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "results"
CLOSED_SHARE = 0.25
MIN_TRAINS = 300


def detect():
    lc = leg_counts(YEAR)
    lc = lc[lc.cat.isin(["FV", "NV", "S"])]
    lc["a"] = np.where(lc.node < lc.next_node, lc.node, lc.next_node)
    lc["b"] = np.where(lc.node < lc.next_node, lc.next_node, lc.node)
    m = lc.groupby(["a", "b", "month"]).trains.sum().unstack("month").reindex(columns=YEAR).fillna(0)
    med = m.median(axis=1)
    m = m[med >= MIN_TRAINS]
    closed = m.lt(CLOSED_SHARE * med[m.index], axis=0)
    net = load_network()
    rows = []
    for (a, b), row in closed.iterrows():
        months = [mo for mo in YEAR if row[mo]]
        if not months or len(months) > 8:
            continue
        try:
            path = [s for s, _ in net.shortest_path(a, b, "NV")]
        except ValueError:
            try:
                path = [s for s, _ in net.shortest_path(a, b, "FV")]
            except ValueError:
                continue
        rows.append({"a": a, "b": b, "months": " ".join(months), "median_trains": med[(a, b)],
                     "min_trains": m.loc[(a, b), months].min(), "segments": " ".join(path)})
    legs = pd.DataFrame(rows)
    if legs.empty:
        legs.to_csv(RES / "closure_events.csv", index=False)
        return legs
    # events: corridors closed in the same months; a corridor counts when every leg using it is closed
    events = []
    for months, g in legs.groupby("months"):
        segs = sorted({s for p in g.segments for s in p.split()})
        events.append({"event": f"E{len(events) + 1}", "months": months,
                       "segments": " ".join(segs), "legs": "; ".join(f"{r.a}-{r.b}" for r in g.itertuples()),
                       "name": ", ".join(segment_name(net, s) for s in segs)})
    ev = pd.DataFrame(events)
    ev.to_csv(RES / "closure_events.csv", index=False)
    return ev


# ---------------------------------------------------------------- backtest
_NET = _TR = _DM = None


def _init(closed_segments):
    global _NET, _TR, _DM
    _NET = load_network()
    for sid in closed_segments:
        _NET.segments.pop(sid, None)
    _NET.rebuild()
    _TR = build_trains(_NET)
    plan_timetable(_NET, _TR)
    _DM = DisruptionModel()


def _day(seed):
    r = simulate_day(_NET, _TR, _DM, seed)
    return {k: r[k] for k in ("punct_FV", "punct_NV", "node_stats")}


def _simulate(closed, days, procs, seed0=5000):
    with Pool(procs, initializer=_init, initargs=(tuple(closed),)) as pool:
        return pool.map(_day, range(seed0, seed0 + days))


def observed_change(closed_months):
    """Station punctuality in the closure months minus the other months (model trips), and the
    national change per category."""
    def load(months):
        pn = pd.concat([pd.read_csv(month_dir(m) / "punctuality_by_node.csv") for m in months])
        pn = pn.assign(ok=pn.stops * pn.punct_lt6).groupby(["node", "cat"])[["stops", "ok"]].sum()
        pc = pd.concat([pd.read_csv(month_dir(m) / "punctuality_by_category.csv") for m in months])
        pc = pc.assign(ok=pc.stops * pc.punct_lt6).groupby("cat")[["stops", "ok"]].sum()
        return pn.ok / pn.stops, pn.stops, pc.ok / pc.stops
    other = [m for m in YEAR if m not in closed_months]
    pc_n, n_c, nat_c = load(closed_months)
    pc_o, n_o, nat_o = load(other)
    d = pd.DataFrame({"obs_change": pc_n - pc_o, "stops_closed": n_c, "stops_other": n_o}).dropna()
    return d.reset_index(), (nat_c - nat_o).to_dict()


def backtest(days=40, procs=4):
    ev = pd.read_csv(RES / "closure_events.csv")
    base = _simulate([], days, procs)
    nb = V.node_table(base, days)
    nat_b = {c: np.mean([o[f"punct_{c}"] for o in base]) for c in ("FV", "NV")}
    rows, summ = [], []
    for e in ev.itertuples():
        segs = e.segments.split()
        months = e.months.split()
        clo = _simulate(segs, days, procs)
        nc = V.node_table(clo, days)
        nat_c = {c: np.mean([o[f"punct_{c}"] for o in clo]) for c in ("FV", "NV")}
        sim = nb.merge(nc, on=["node", "cat"], suffixes=("_base", "_closed"))
        sim["sim_did"] = sim.sim_punct_closed - sim.sim_punct_base - sim.cat.map(
            {c: nat_c[c] - nat_b[c] for c in nat_c})
        obs, nat_o = observed_change(months)
        obs["obs_did"] = obs.obs_change - obs.cat.map(nat_o)
        m = sim.merge(obs, on=["node", "cat"])
        m = m[m.cat.isin(["FV", "NV"]) & (m.stops_closed >= 100) & (m.stops_other >= 100)]
        net = load_network()
        near = set()
        for s in segs:
            near |= {net.segments[s].a, net.segments[s].b}
        m["near"] = m.node.isin(near)
        m["event"] = e.event
        rows.append(m)
        summ.append({"event": e.event, "name": e.name, "months": e.months, "n": int(len(m)),
                     "corr": float(np.corrcoef(m.sim_did, m.obs_did)[0, 1]) if len(m) > 3 else None,
                     "corr_near": float(np.corrcoef(m[m.near].sim_did, m[m.near].obs_did)[0, 1])
                     if m.near.sum() > 3 else None})
        print(summ[-1], flush=True)
    allm = pd.concat(rows, ignore_index=True)
    allm.to_csv(RES / "backtest_events.csv", index=False)
    # pooled: stations near a closure (largest expected signal) and all stations
    big = allm[allm.sim_did.abs() >= 0.01]
    out = {"days": days, "events": summ,
           "pooled_corr_all": float(np.corrcoef(allm.sim_did, allm.obs_did)[0, 1]),
           "pooled_corr_near": float(np.corrcoef(allm[allm.near].sim_did, allm[allm.near].obs_did)[0, 1]),
           "pooled_corr_simulated_effect_ge_1pp": float(np.corrcoef(big.sim_did, big.obs_did)[0, 1])
           if len(big) > 3 else None,
           "sign_agreement_simulated_effect_ge_1pp": float((np.sign(big.sim_did) == np.sign(big.obs_did)).mean())
           if len(big) else None,
           "n_pairs": int(len(allm)), "n_near": int(allm.near.sum()), "n_big": int(len(big))}
    (RES / "backtest_events.json").write_text(json.dumps(out, indent=2))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--procs", type=int, default=4)
    a = ap.parse_args()
    if a.backtest:
        print(json.dumps(backtest(a.days, a.procs), indent=2))
    else:
        ev = detect()
        pd.set_option("display.width", 220)
        print(ev.to_string(index=False))


if __name__ == "__main__":
    main()
