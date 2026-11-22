"""Combine monthly observed statistics (data/observed_YYYY-MM/) into multi-month sets.

* ``data/observed_cal``: calibration months (even months of the year Nov 2025 - Oct 2026)
* ``data/observed_val``: validation months (odd months) - out-of-sample, every season in both sets
* ``data/observed_year``: all twelve months

All statistics are combined with the right weights (stops, trains, counts).

    python -m golden_rails.observed_agg
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
YEAR = ["2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06",
        "2026-07", "2026-08", "2026-09", "2026-10"]
SETS = {"observed_cal": [m for m in YEAR if int(m[-2:]) % 2 == 0],
        "observed_val": [m for m in YEAR if int(m[-2:]) % 2 == 1],
        "observed_year": YEAR}


def month_dir(m):
    return DATA / f"observed_{m}"


def _wavg(df, keys, weight, cols):
    def f(g):
        w = g[weight].to_numpy(float)
        out = {weight: w.sum()}
        for c in cols:
            v = g[c].to_numpy(float)
            ok = np.isfinite(v)
            out[c] = float(np.average(v[ok], weights=w[ok])) if ok.any() and w[ok].sum() > 0 else np.nan
        return pd.Series(out)
    return df.groupby(keys).apply(f, include_groups=False).reset_index()


def combine(months, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    read = lambda name: pd.concat([pd.read_csv(month_dir(m) / name).assign(month=m) for m in months
                                   if (month_dir(m) / name).exists()], ignore_index=True)
    pc = read("punctuality_by_category.csv")
    _wavg(pc, ["cat"], "stops", ["cancelled_share", "punct_lt6", "punct_lt16", "mean_delay"]).to_csv(
        out / "punctuality_by_category.csv", index=False)
    for name in ("punctuality_by_node.csv", "punctuality_by_node_all.csv"):
        pn = read(name)
        _wavg(pn, ["node", "cat"], "stops", ["punct_lt6", "mean_delay"]).to_csv(out / name, index=False)
    lg = read("delay_change_by_leg.csv")
    _wavg(lg, ["node", "next_node", "cat"], "trains", ["mean_change", "p_gain_ge3", "mean_dep_delay"]).to_csv(
        out / "delay_change_by_leg.csv", index=False)
    for name in ("entry_delay_hist.csv", "origin_delay_hist.csv"):
        h = read(name)
        h.groupby(["cat", "d"]).n.sum().reset_index().to_csv(out / name, index=False)
    en = read("entry_delay_by_node.csv")
    en["late"] = en.trains * en.p_delayed_ge1
    agg = en.groupby(["node", "cat"]).apply(lambda g: pd.Series({
        "trains": g.trains.sum(), "p_delayed_ge1": g.late.sum() / g.trains.sum(),
        "mean_if_delayed": np.average(g.mean_if_delayed.fillna(0), weights=g.late + 1e-9)}),
        include_groups=False).reset_index()
    agg.to_csv(out / "entry_delay_by_node.csv", index=False)
    od = read("origin_delay.csv")
    _wavg(od, ["cat"], "trains", [c for c in od.columns if c not in ("cat", "trains", "month")]).to_csv(
        out / "origin_delay.csv", index=False)
    nd = read("node_dwell_gain.csv")
    _wavg(nd, ["node", "cat"], "stops", ["mean_dwell_gain"]).to_csv(out / "node_dwell_gain.csv", index=False)
    ca = read("cancellations.csv")
    ca["canc"] = ca.stops * ca.cancelled_stop_share
    rows = []
    for c, g in ca.groupby("cat"):
        rows.append({"cat": c, "trips": g.trips.sum(), "stops": g.stops.sum(),
                     "cancelled_stop_share": g.canc.sum() / g.stops.sum(),
                     **{k: float(np.average(g[k], weights=g.canc)) for k in ("share_full", "share_tail", "share_other")},
                     **{k: float(np.average(g[k], weights=g.trips)) for k in ("trips_full", "trips_tail", "trips_other")}})
    pd.DataFrame(rows).to_csv(out / "cancellations.csv", index=False)
    ta = read("turnaround.csv")
    _wavg(ta, ["cat", "arr_bin"], "n", ["mean_turn", "mean_arr", "mean_dep", "p_dep_ge3", "p_dep_ge6"]).to_csv(
        out / "turnaround.csv", index=False)
    meta = json.loads((month_dir(months[-1]) / "meta.json").read_text())
    meta.update({"month": f"{months[0]}..{months[-1]} ({len(months)} Monate)", "months": months,
                 "rows_used": None})
    (out / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))


def leg_counts(months) -> pd.DataFrame:
    """Trains per month and leg (for detecting closures)."""
    rows = []
    for m in months:
        f = month_dir(m) / "delay_change_by_leg.csv"
        if f.exists():
            rows.append(pd.read_csv(f).assign(month=m))
    return pd.concat(rows, ignore_index=True)


def main():
    for name, months in SETS.items():
        have = [m for m in months if (month_dir(m) / "meta.json").exists()]
        if len(have) < len(months):
            print(f"{name}: missing {sorted(set(months) - set(have))}", file=sys.stderr)
        combine(have, DATA / name)
        print(name, have)


if __name__ == "__main__":
    main()
