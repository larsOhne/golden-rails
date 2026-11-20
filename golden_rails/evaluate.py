"""Evaluate corridor upgrades: passenger-delay minutes saved per euro.

KPI = cumulative passenger delay saved per year
      / (capex annuity + opex)          [passenger-delay-min per EUR 1,000]

Passenger delay = sum over trains and stops of (arrival delay x passengers
expected to alight there). Because demand is held fixed, ranking by this is
identical to ranking by average delay per expected passenger.

Options per corridor (segment): +1 track, +2 tracks, ETCS, +1 track with ETCS.
Two stages: screen everything with few Monte-Carlo days, then refine the best.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

from .costs import etcs_upgrade_cost, track_addition_cost
from .network import DATA, load_network, segment_name
from .sim import DAY_FACTOR, TYPES, DisruptionModel, build_trains, plan_timetable, simulate_day

ROOT = Path(__file__).resolve().parent.parent

DISCOUNT_RATE = 0.017        # BVWP 2030 social discount rate
LIFETIME_YEARS = 60
OPEX_EUR_PER_TRACK_KM = 130_000   # maintenance + renewal reserve per added track-km and year


def annuity_factor(r=DISCOUNT_RATE, n=LIFETIME_YEARS):
    return r / (1 - (1 + r) ** -n)


def corridor_options(net) -> pd.DataFrame:
    rows = []
    for s in net.segments.values():
        nm = segment_name(net, s.id)
        c1 = track_addition_cost(s, 1)
        c2 = track_addition_cost(s, 2)
        opts = [
            ("T1", f"+1 Gleis {nm} ({s.tracks}→{s.tracks + 1})", f"tracks:{s.id}:{s.tracks + 1}", c1),
            ("T2", f"+2 Gleise {nm} ({s.tracks}→{s.tracks + 2})", f"tracks:{s.id}:{s.tracks + 2}", c2),
        ]
        if not s.etcs:
            ce = etcs_upgrade_cost(s)
            opts.append(("E", f"ETCS {nm}", f"etcs:{s.id}", ce))
            both = {k: c1[k] + ce[k] for k in ("construction", "land", "etcs", "total")}
            opts.append(("T1E", f"+1 Gleis + ETCS {nm}", f"tracks:{s.id}:{s.tracks + 1};etcs:{s.id}", both))
        for code, name, changes, c in opts:
            rows.append({"id": f"{code}-{s.id}", "name": name, "changes": changes, "segment": s.id,
                         "option": code, "capex_meur": c["total"],
                         "capex_construction": c["construction"], "capex_land": c["land"],
                         "capex_etcs": c["etcs"], "source_note": "cost model (terrain, land use, ETCS)"})
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


METRICS = ["delay_min", "pax_delay_min", "avg_delay_per_pax"] + \
          [f"pax_delay_{k}" for k in TYPES] + [f"delay_{k}" for k in TYPES] + \
          [f"punct_{k}" for k in TYPES]


def run_scenario(args):
    sid, changes, seeds = args
    net = load_network()
    base_net = load_network()
    added_track_km, route_km = 0.0, 0.0
    if changes:
        added_track_km = net.apply_changes(changes)
        for op in changes.split(";"):
            p = op.split(":")
            if p[0] == "new":
                route_km += float(p[3])
            elif p[0] == "tracks":
                route_km += base_net.segments[p[1]].length
    trains = build_trains(net)
    plan = plan_timetable(net, trains, base_plan())
    runs, seg_wait = [], {}
    for s in seeds:
        r = simulate_day(net, trains, _DM, s)
        for k, v in r.pop("seg_wait").items():
            seg_wait[k] = seg_wait.get(k, 0.0) + v / len(seeds)
        runs.append({k: r[k] for k in METRICS})
    return {"id": sid, "added_track_km": added_track_km, "route_km": route_km,
            "n_replanned": plan["n_replanned"], "runs": runs, "seg_wait": seg_wait}


def run_many(tasks, dm, procs):
    with Pool(procs, initializer=_init, initargs=(dm.to_dict(),)) as pool:
        return {r["id"]: r for r in pool.map(run_scenario, tasks, chunksize=1)}


def summarise(cands: pd.DataFrame, res: dict, base: dict) -> pd.DataFrame:
    af = annuity_factor()
    b_pax = np.array([x["pax_delay_min"] for x in base["runs"]])
    b_tr = np.array([x["delay_min"] for x in base["runs"]])
    rows = []
    for c in cands.itertuples(index=False):
        r = res[c.id]
        n = len(r["runs"])
        pax = b_pax[:n] - np.array([x["pax_delay_min"] for x in r["runs"]])
        tr = b_tr[:n] - np.array([x["delay_min"] for x in r["runs"]])
        se = pax.std(ddof=1) / math.sqrt(n)
        annual_cost = c.capex_meur * 1e6 * af + r["added_track_km"] * OPEX_EUR_PER_TRACK_KM
        per_keur = lambda v: v * DAY_FACTOR / annual_cost * 1000
        row = c._asdict()
        row.update({
            "route_km": r["route_km"], "added_track_km": r["added_track_km"],
            "annual_cost_meur": annual_cost / 1e6, "n_seeds": n,
            "pax_min_saved_per_day": pax.mean(), "se_per_day": se,
            "pax_min_saved_per_year_m": pax.mean() * DAY_FACTOR / 1e6,
            "train_min_saved_per_day": tr.mean(),
            "kpi_paxmin_per_keur": per_keur(pax.mean()),
            "kpi_lo": per_keur(pax.mean() - 1.96 * se),
            "kpi_hi": per_keur(pax.mean() + 1.96 * se),
            "eur_per_pax_min": annual_cost / (pax.mean() * DAY_FACTOR) if pax.mean() > 0 else math.inf,
            "avg_delay_per_pax_change_s": 60 * np.mean([y["avg_delay_per_pax"] - x["avg_delay_per_pax"]
                                                        for x, y in zip(r["runs"], base["runs"])]),
            "n_trains_replanned": r["n_replanned"],
        })
        for k in TYPES:
            row[f"pax_saved_{k}_day"] = np.mean([y[f"pax_delay_{k}"] - x[f"pax_delay_{k}"]
                                                 for x, y in zip(r["runs"], base["runs"])])
            row[f"train_saved_{k}_day"] = np.mean([y[f"delay_{k}"] - x[f"delay_{k}"]
                                                   for x, y in zip(r["runs"], base["runs"])])
        rows.append(row)
    return pd.DataFrame(rows).sort_values("kpi_paxmin_per_keur", ascending=False)


def evaluate(screen_seeds=16, refine_seeds=60, refine_top=30, procs=4,
             dm: DisruptionModel | None = None, out_dir=ROOT / "results", tag="",
             include_generic=True) -> pd.DataFrame:
    dm = dm or DisruptionModel()
    net = load_network()
    planned = pd.read_csv(DATA / "candidates.csv")
    planned["kind"] = "planned project"
    planned["segment"], planned["option"] = "", "P"
    for k in ("capex_construction", "capex_land", "capex_etcs"):
        planned[k] = np.nan
    cands = planned
    if include_generic:
        g = corridor_options(net)
        g["kind"] = "corridor option"
        cands = pd.concat([planned, g], ignore_index=True)

    t0 = time.time()
    s_seeds = list(range(1000, 1000 + screen_seeds))
    res = run_many([("BASE", "", s_seeds)] + [(c.id, c.changes, s_seeds) for c in cands.itertuples()],
                   dm, procs)
    screen = summarise(cands, res, res["BASE"])
    print(f"screening: {len(cands)} scenarios x {screen_seeds} days in {time.time() - t0:.0f}s", flush=True)

    # refine: top-N corridor options + all planned projects
    top = set(screen[screen.kind == "corridor option"].head(refine_top).id) | set(planned.id)
    rc = cands[cands.id.isin(top)]
    r_seeds = list(range(1000, 1000 + refine_seeds))
    res2 = run_many([("BASE", "", r_seeds)] + [(c.id, c.changes, r_seeds) for c in rc.itertuples()],
                    dm, procs)
    refined = summarise(rc, res2, res2["BASE"])
    print(f"refinement: {len(rc)} scenarios x {refine_seeds} days, total {time.time() - t0:.0f}s", flush=True)

    out_dir.mkdir(exist_ok=True, parents=True)
    screen.to_csv(out_dir / f"screening{tag}.csv", index=False)
    refined.to_csv(out_dir / f"candidates{tag}.csv", index=False)
    base = res2["BASE"]["runs"]
    summary = {
        "screen_seeds": screen_seeds, "refine_seeds": refine_seeds,
        "disruption_model": dm.to_dict(),
        "baseline_pax_delay_min_per_day": float(np.mean([x["pax_delay_min"] for x in base])),
        "baseline_train_delay_min_per_day": float(np.mean([x["delay_min"] for x in base])),
        "baseline_avg_delay_per_pax_min": float(np.mean([x["avg_delay_per_pax"] for x in base])),
        "baseline_punct": {k: float(np.mean([x[f"punct_{k}"] for x in base])) for k in TYPES},
        "annuity_factor": annuity_factor(),
    }
    (out_dir / f"summary{tag}.json").write_text(json.dumps(summary, indent=2))
    hot = pd.DataFrame(sorted(res2["BASE"]["seg_wait"].items(), key=lambda kv: -kv[1]),
                       columns=["segment", "knock_on_wait_min_per_day"])
    hot.to_csv(out_dir / f"baseline_hotspots{tag}.csv", index=False)
    return refined


def refine(option_codes, top=20, seeds=60, procs=4, dm=None, tag="", out_dir=ROOT / "results",
           extra_ids=()):
    """Refine the best screened options of the given types (e.g. track building only)."""
    dm = dm or DisruptionModel()
    screen = pd.read_csv(out_dir / "screening.csv")
    sel = screen[screen.option.isin(option_codes)].head(top)
    sel = pd.concat([sel, screen[screen.id.isin(extra_ids)]]).drop_duplicates("id")
    cols = [c for c in screen.columns if c in ("id", "name", "changes", "capex_meur", "source_note", "kind",
                                               "segment", "option", "capex_construction", "capex_land",
                                               "capex_etcs")]
    rc = sel[cols].reset_index(drop=True)
    r_seeds = list(range(1000, 1000 + seeds))
    res = run_many([("BASE", "", r_seeds)] + [(c.id, c.changes, r_seeds) for c in rc.itertuples()], dm, procs)
    out = summarise(rc, res, res["BASE"])
    # incremental value of a track on a corridor that already got ETCS
    inc = []
    af = annuity_factor()
    for seg in rc.segment.dropna().unique():
        e, te = f"E-{seg}", f"T1E-{seg}"
        if e in res and te in res:
            d = np.array([a["pax_delay_min"] - b["pax_delay_min"] for a, b in zip(res[e]["runs"], res[te]["runs"])])
            c1 = rc.set_index("id").loc[te, "capex_meur"] - rc.set_index("id").loc[e, "capex_meur"]
            cost = c1 * 1e6 * af + res[te]["added_track_km"] * OPEX_EUR_PER_TRACK_KM
            inc.append({"segment": seg, "incremental_pax_min_per_year_m": d.mean() * DAY_FACTOR / 1e6,
                        "incremental_kpi_paxmin_per_keur": d.mean() * DAY_FACTOR / cost * 1000,
                        "se_per_day": d.std(ddof=1) / math.sqrt(len(d))})
    out.to_csv(out_dir / f"candidates{tag}.csv", index=False)
    if inc:
        pd.DataFrame(inc).sort_values("incremental_kpi_paxmin_per_keur", ascending=False).to_csv(
            out_dir / f"track_after_etcs{tag}.csv", index=False)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--screen-seeds", type=int, default=16)
    ap.add_argument("--seeds", type=int, default=60, help="Monte-Carlo days in refinement")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--no-generic", action="store_true")
    ap.add_argument("--incident-scale", type=float, default=1.0)
    ap.add_argument("--tag", default="")
    ap.add_argument("--refine-options", default="", help="e.g. T1,T2,T1E: refine only these option types "
                                                         "from results/screening.csv")
    ap.add_argument("--extra", default="", help="comma-separated extra ids to refine")
    a = ap.parse_args()
    dm = DisruptionModel()
    dm.rate_full_closure *= a.incident_scale
    dm.rate_track_fault *= a.incident_scale
    if a.refine_options:
        df = refine(a.refine_options.split(","), a.top, a.seeds, a.procs, dm, a.tag,
                    extra_ids=[x for x in a.extra.split(",") if x])
    else:
        df = evaluate(a.screen_seeds, a.seeds, a.top, a.procs, dm, tag=a.tag,
                      include_generic=not a.no_generic)
    pd.set_option("display.width", 250)
    print(df[["id", "name", "route_km", "capex_meur", "pax_min_saved_per_year_m",
              "kpi_paxmin_per_keur", "kpi_lo", "kpi_hi", "eur_per_pax_min"]].head(25).to_string(index=False))


if __name__ == "__main__":
    main()
