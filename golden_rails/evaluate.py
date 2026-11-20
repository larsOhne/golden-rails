"""Evaluate candidate infrastructure: delay minutes saved per euro (capex annuity + opex)."""
from __future__ import annotations

import argparse
import json
import math
import time
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

from .network import DATA, load_network
from .sim import DAY_FACTOR, DisruptionModel, build_trains, plan_timetable, simulate_day

ROOT = Path(__file__).resolve().parent.parent

# Economic parameters
DISCOUNT_RATE = 0.017        # BVWP 2030 social discount rate
LIFETIME_YEARS = 60          # civil works / track bed
OPEX_EUR_PER_TRACK_KM = 130_000   # maintenance + renewal reserve per track-km and year


def annuity_factor(r=DISCOUNT_RATE, n=LIFETIME_YEARS):
    return r / (1 - (1 + r) ** -n)


def generic_candidates(net) -> pd.DataFrame:
    """'+1 track' on every existing segment that has fewer than 4 tracks."""
    rows = []
    for s in net.segments.values():
        if s.tracks >= 4:
            continue
        rows.append({
            "id": f"G-{s.id}",
            "name": f"+1 track {s.id} ({s.tracks}->{s.tracks + 1})",
            "changes": f"tracks:{s.id}:{s.tracks + 1}",
            "capex_meur": s.length * s.upgrade_meur_per_km,
            "source_note": "generic upgrade, unit cost from segments.csv",
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
_DM: DisruptionModel | None = None
_BASE_PLAN: dict | None = None


def base_plan():
    """Current timetable (planned once per process, deterministic)."""
    global _BASE_PLAN
    if _BASE_PLAN is None:
        net = load_network()
        trains = build_trains(net)
        plan_timetable(net, trains)
        _BASE_PLAN = {tr.id: tr for tr in trains}
    return _BASE_PLAN


def _init(dm_dict):
    global _DM
    _DM = DisruptionModel(**dm_dict)


def run_scenario(args):
    sid, changes, seeds = args
    net = load_network()
    added_track_km, new_route_km = 0.0, 0.0
    if changes:
        before = set(net.segments)
        added_track_km = net.apply_changes(changes)
        for op in changes.split(";"):
            p = op.split(":")
            if p[0] == "new":
                new_route_km += float(p[3])
            else:
                seg = load_network().segments[p[1]]
                new_route_km += seg.length
    t0 = time.time()
    trains = build_trains(net)
    plan = plan_timetable(net, trains, base_plan())
    out = []
    seg_wait = {}
    for s in seeds:
        r = simulate_day(net, trains, _DM, s)
        for k, v in r.pop("seg_wait").items():
            seg_wait[k] = seg_wait.get(k, 0.0) + v / len(seeds)
        out.append(r)
    return {
        "id": sid, "added_track_km": added_track_km, "new_route_km": new_route_km,
        "planned_wait_min": plan["planned_wait_min"], "n_replanned": plan["n_replanned"],
        "runs": out,
        "seg_wait": seg_wait, "secs": time.time() - t0,
    }


def evaluate(n_seeds=40, procs=4, dm: DisruptionModel | None = None,
             include_generic=True, out_dir=ROOT / "results", tag="") -> pd.DataFrame:
    dm = dm or DisruptionModel()
    net = load_network()
    cands = pd.read_csv(DATA / "candidates.csv")
    cands["kind"] = "planned project"
    if include_generic:
        g = generic_candidates(net)
        g["kind"] = "generic +1 track"
        cands = pd.concat([cands, g], ignore_index=True)
    seeds = list(range(1000, 1000 + n_seeds))
    tasks = [("BASE", "", seeds)] + [(r.id, r.changes, seeds) for r in cands.itertuples()]
    with Pool(procs, initializer=_init, initargs=(dm.to_dict(),)) as pool:
        res = pool.map(run_scenario, tasks, chunksize=1)
    by_id = {r["id"]: r for r in res}
    base = by_id["BASE"]
    base_delay = np.array([r["delay_min"] for r in base["runs"]])

    af = annuity_factor()
    rows = []
    for c in cands.itertuples():
        r = by_id[c.id]
        d = np.array([x["delay_min"] for x in r["runs"]])
        saved = base_delay - d                       # per day, paired (CRN)
        saved_day, se_day = saved.mean(), saved.std(ddof=1) / math.sqrt(len(saved))
        saved_year = saved_day * DAY_FACTOR
        capex = c.capex_meur * 1e6
        annual_cost = capex * af + r["added_track_km"] * OPEX_EUR_PER_TRACK_KM
        kpi = saved_year / annual_cost * 1000      # delay-min saved per 1000 EUR/yr
        comp = {}
        for k in ("FV", "NV", "GV"):
            comp[f"saved_{k}_day"] = np.mean([b[f"delay_{k}"] - x[f"delay_{k}"]
                                              for b, x in zip(base["runs"], r["runs"])])
        comp["saved_final_arrival_day"] = np.mean([
            sum(b[f"final_delay_{k}"] - x[f"final_delay_{k}"] for k in ("FV", "NV", "GV"))
            for b, x in zip(base["runs"], r["runs"])])
        rows.append({
            "id": c.id, "name": c.name, "kind": c.kind, "changes": c.changes,
            "route_km": r["new_route_km"], "added_track_km": r["added_track_km"],
            "capex_meur": c.capex_meur,
            "annual_cost_meur": annual_cost / 1e6,
            "saved_min_per_day": saved_day, "se_min_per_day": se_day,
            "saved_min_per_year_m": saved_year / 1e6,
            "kpi_min_per_keur": kpi,
            "kpi_lo": (saved_day - 1.96 * se_day) * DAY_FACTOR / annual_cost * 1000,
            "kpi_hi": (saved_day + 1.96 * se_day) * DAY_FACTOR / annual_cost * 1000,
            "eur_per_min_saved": annual_cost / saved_year if saved_year > 0 else math.inf,
            "saved_min_per_year_per_km": saved_year / max(r["new_route_km"], 1e-9),
            "punct_FV": np.mean([x["punct_FV"] for x in r["runs"]]),
            **comp,
            "n_trains_replanned": r["n_replanned"],
        })
    df = pd.DataFrame(rows).sort_values("kpi_min_per_keur", ascending=False)
    out_dir.mkdir(exist_ok=True, parents=True)
    df.to_csv(out_dir / f"candidates{tag}.csv", index=False)
    summary = {
        "n_seeds": n_seeds,
        "disruption_model": dm.to_dict(),
        "baseline_delay_min_per_day": float(base_delay.mean()),
        "baseline_delay_min_per_year_m": float(base_delay.mean() * DAY_FACTOR / 1e6),
        "baseline_punct": {k: float(np.mean([x[f"punct_{k}"] for x in base["runs"]]))
                           for k in ("FV", "NV", "GV")},
        "annuity_factor": af,
    }
    (out_dir / f"summary{tag}.json").write_text(json.dumps(summary, indent=2))
    hot = pd.DataFrame(sorted(base["seg_wait"].items(), key=lambda kv: -kv[1]),
                       columns=["segment", "knock_on_wait_min_per_day"])
    hot.to_csv(out_dir / f"baseline_hotspots{tag}.csv", index=False)
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=40)
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--no-generic", action="store_true")
    ap.add_argument("--incident-scale", type=float, default=1.0)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    dm = DisruptionModel()
    dm.rate_full_closure *= a.incident_scale
    dm.rate_track_fault *= a.incident_scale
    t = time.time()
    df = evaluate(a.seeds, a.procs, dm, not a.no_generic, tag=a.tag)
    pd.set_option("display.width", 250)
    print(df[["id", "name", "route_km", "capex_meur", "saved_min_per_day", "se_min_per_day",
              "kpi_min_per_keur", "eur_per_min_saved"]].head(30).to_string(index=False))
    print(f"done in {time.time() - t:.0f}s")


if __name__ == "__main__":
    main()
