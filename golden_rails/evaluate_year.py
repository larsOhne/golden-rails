"""Evaluate measures over the year: every simulated day runs on one of the 12 monthly timetables.

KPI = passenger-delay minutes saved per year (incl. cancellations) / annual cost [per EUR 1,000].
Scenarios are compared with the baseline on the same random days (common random numbers) and the same
monthly timetables; the current timetable is kept, only trains with changed routes are re-planned.

Stages:
  screen   all measures of the catalogue (measures.py), SCREEN_PER_MONTH days per monthly timetable
  refine   the best measures and all planned projects, REFINE_PER_MONTH days per month
  greedy   portfolio built step by step: in each step the marginal value of the best remaining
           candidates is re-evaluated on top of the portfolio (interactions: ETCS <-> fleet retrofit,
           tracks <-> ETCS, node <-> corridor)

    python -m golden_rails.evaluate_year screen
    python -m golden_rails.evaluate_year refine
    python -m golden_rails.evaluate_year greedy --steps 12
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

from . import measures as M
from . import sim as S
from . import timetable_opt
from .network import load_network
from .observed_agg import YEAR
from .sim import DAY_FACTOR, TYPES, DisruptionModel, build_trains, plan_timetable, simulate_day

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "results"
TT = ROOT / "data" / "timetables"
SCREEN_PER_MONTH = 2
REFINE_PER_MONTH = 10
GREEDY_PER_MONTH = 3
REF_MONTH = "2026-05"
METRICS = ["delay_min", "pax_delay_min", "pax_delay_cancel_min", "avg_delay_per_pax", "pax_punct"] + \
          [f"pax_delay_{k}" for k in TYPES] + [f"punct_{k}" for k in TYPES]

_W: dict = {}


def months():
    return [m for m in YEAR if S.month_timetable(m)[0].exists()]


def shifts_file(m):
    return TT / f"shifts_{m}.json"


def prepare_shifts():
    """Timetable softening per month (optimised on the planned current timetable)."""
    for m in months():
        if shifts_file(m).exists():
            continue
        tt, bg = S.month_timetable(m)
        net = load_network()
        net.bg_file = str(bg)
        trains = build_trains(net, tt_file=tt)
        plan_timetable(net, trains)
        sh = timetable_opt.optimise(net, trains, passes=3, log=lambda *_: None)
        shifts_file(m).write_text(json.dumps(sh))
        print(f"shifts {m}: {len(sh)} trains moved", flush=True)


def _init(dm_dict):
    _W.clear()
    _W["dm"] = DisruptionModel(**dm_dict)
    for m in months():
        tt, bg = S.month_timetable(m)
        sh = json.loads(shifts_file(m).read_text()) if shifts_file(m).exists() else {}
        for soft in (False, True):
            net = load_network()
            net.bg_file = str(bg)
            trains = build_trains(net, tt_file=tt, shifts=sh if soft else None)
            plan_timetable(net, trains)
            _W[(m, soft)] = {t.id: t for t in trains}
        _W[("shifts", m)] = sh


def _scenario(task):
    sid, changes, per_month, seed0 = task
    soft = "tt:soft" in changes
    ch = ";".join(op for op in changes.split(";") if op and op != "tt:soft")
    runs, added, route_km, replanned = [], 0.0, 0.0, 0
    for mi, m in enumerate(months()):
        tt, bg = S.month_timetable(m)
        net = load_network()
        net.bg_file = str(bg)
        if ch:
            added = net.apply_changes(ch)
        trains = build_trains(net, tt_file=tt, shifts=_W[("shifts", m)] if soft else None)
        info = plan_timetable(net, trains, _W[(m, soft)])
        replanned += info["n_replanned"]
        for k in range(per_month):
            r = simulate_day(net, trains, _W["dm"], seed0 + 1000 * mi + k)
            runs.append({x: r[x] for x in METRICS})
    return {"id": sid, "runs": runs, "added_track_km": added, "n_replanned": replanned}


CKPT = RES / "ckpt"


def _key(task):
    sid, changes, per_month, seed0 = task
    return f"{sid}|{changes}|{per_month}|{seed0}"


def run_many(tasks, procs=4, dm=None, ckpt="default"):
    """Run scenarios in parallel. Finished scenarios are appended to a checkpoint file
    (results/ckpt/<ckpt>.jsonl), so an interrupted run resumes where it stopped."""
    dm = dm or DisruptionModel()
    CKPT.mkdir(parents=True, exist_ok=True)
    f = CKPT / f"{ckpt}.jsonl"
    done = {}
    if f.exists():
        for line in f.read_text().splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue          # truncated last line of an interrupted run
            done[d["key"]] = d["res"]
    keys = {_key(t): t for t in tasks}
    todo = [t for k, t in keys.items() if k not in done]
    if todo:
        print(f"{ckpt}: {len(keys) - len(todo)} cached, {len(todo)} to run", flush=True)
        with open(f, "a") as fh, Pool(procs, initializer=_init, initargs=(dm.to_dict(),)) as pool:
            for i, (t, r) in enumerate(zip(todo, pool.imap(_scenario, todo)), 1):
                done[_key(t)] = r
                fh.write(json.dumps({"key": _key(t), "res": r}) + "\n")
                fh.flush()
                if i % 20 == 0:
                    print(f"{ckpt}: {i}/{len(todo)}", flush=True)
    return {t[0]: done[k] for k, t in keys.items()}


def summarise(cands: pd.DataFrame, res: dict, base_id="BASE", base_cost=None) -> pd.DataFrame:
    base = res[base_id]["runs"]
    b_pax = np.array([x["pax_delay_min"] for x in base])
    rows = []
    for c in cands.itertuples(index=False):
        if c.id not in res:
            continue
        r = res[c.id]["runs"]
        pax = b_pax[:len(r)] - np.array([x["pax_delay_min"] for x in r])
        n = len(pax)
        se = pax.std(ddof=1) / math.sqrt(n)
        annual = float(c.annual_cost_meur)
        if c.option == "P":
            annual += res[c.id]["added_track_km"] * M.TRACK_OPEX_EUR_TKM / 1e6
        per_keur = (lambda v: v * DAY_FACTOR / (annual * 1e6) * 1000) if annual > 0 else (lambda v: math.inf)
        row = c._asdict()
        row.update({"annual_cost_meur": annual, "n_days": n,
                    "pax_min_saved_per_day": pax.mean(), "se_per_day": se,
                    "pax_min_saved_per_year_m": pax.mean() * DAY_FACTOR / 1e6,
                    "kpi_paxmin_per_keur": per_keur(pax.mean()),
                    "kpi_lo": per_keur(pax.mean() - 1.96 * se), "kpi_hi": per_keur(pax.mean() + 1.96 * se),
                    "cancel_min_saved_per_day": np.mean([y["pax_delay_cancel_min"] - x["pax_delay_cancel_min"]
                                                         for x, y in zip(r, base)]),
                    "punct_FV_change_pp": 100 * np.mean([x["punct_FV"] - y["punct_FV"] for x, y in zip(r, base)]),
                    "pax_punct_change_pp": 100 * np.mean([x["pax_punct"] - y["pax_punct"] for x, y in zip(r, base)]),
                    "n_trains_replanned": res[c.id]["n_replanned"]})
        for k in ("FV", "NV", "S"):
            row[f"pax_saved_{k}_day"] = np.mean([y[f"pax_delay_{k}"] - x[f"pax_delay_{k}"] for x, y in zip(r, base)])
        rows.append(row)
    return pd.DataFrame(rows).sort_values("kpi_paxmin_per_keur", ascending=False)


def reference_catalogue():
    tt, bg = S.month_timetable(REF_MONTH)
    net = load_network()
    net.bg_file = str(bg)
    trains = build_trains(net, tt_file=tt)
    return M.catalogue(trains, net)


def screen(procs=4, per_month=SCREEN_PER_MONTH, skip_e2=True):
    prepare_shifts()
    cat = reference_catalogue()
    if skip_e2:     # ETCS-only is evaluated for the best corridors in the refinement
        cat = cat[cat.option != "E2"]
    t0 = time.time()
    tasks = [("BASE", "", per_month, 50_000)] + [(c.id, c.changes, per_month, 50_000) for c in cat.itertuples()]
    res = run_many(tasks, procs, ckpt="screen")
    out = summarise(cat, res)
    out.to_csv(RES / "year_screening.csv", index=False)
    print(f"screening: {len(cat)} measures x {per_month * len(months())} days in {time.time() - t0:.0f}s", flush=True)
    return out


def refine(procs=4, per_month=REFINE_PER_MONTH, top=40):
    scr = pd.read_csv(RES / "year_screening.csv")
    cat = reference_catalogue()
    fin = scr[np.isfinite(scr.kpi_paxmin_per_keur)]
    best = list(fin[fin.option != "P"].head(top).id)
    # ETCS-only variant for the best ETCS corridors, all planned projects, timetable softening
    e_best = [i for i in fin[fin.option == "E"].head(12).id]
    ids = set(best) | {i.replace("E-", "E2-", 1) for i in e_best} | set(cat[cat.option == "P"].id) | {"TT-soft"}
    sel = cat[cat.id.isin(ids)]
    tasks = [("BASE", "", per_month, 70_000)] + [(c.id, c.changes, per_month, 70_000) for c in sel.itertuples()]
    res = run_many(tasks, procs, ckpt="refine")
    out = summarise(sel, res)
    out.to_csv(RES / "year_candidates.csv", index=False)
    rows = []
    b = res["BASE"]["runs"]
    for i in sel.id:
        for k, (x, y) in enumerate(zip(b, res[i]["runs"])):
            rows.append({"id": i, "day": k, "pax_min_saved": x["pax_delay_min"] - y["pax_delay_min"]})
    pd.DataFrame(rows).to_csv(RES / "year_per_day.csv", index=False)
    base = res["BASE"]["runs"]
    summ = {"days": len(base), "months": months(),
            "baseline_pax_delay_min_per_day": float(np.mean([x["pax_delay_min"] for x in base])),
            "baseline_cancel_pax_min_per_day": float(np.mean([x["pax_delay_cancel_min"] for x in base])),
            "baseline_punct": {k: float(np.mean([x[f"punct_{k}"] for x in base])) for k in TYPES},
            "baseline_pax_punct": float(np.mean([x["pax_punct"] for x in base]))}
    (RES / "year_summary.json").write_text(json.dumps(summ, indent=2))
    return out


def _conflicts(a, b):
    """Measures that cannot be combined (same corridor: T1/T2, E/E2; same node+group reserve sizes)."""
    sa, sb = a.get("segment"), b.get("segment")
    if isinstance(sa, str) and sa and sa == sb:
        pa, pb = a["option"], b["option"]
        if {pa, pb} <= {"T1", "T2"} or {pa, pb} <= {"E", "E2"}:
            return True
    if a["option"] == b["option"] == "RES" and a.get("node") == b.get("node") and a.get("group") == b.get("group"):
        return True
    return a["id"] == b["id"]


def greedy(procs=4, steps=12, pool_size=24, per_month=GREEDY_PER_MONTH):
    """Portfolio built step by step by marginal passenger-delay saving per euro."""
    cand = pd.read_csv(RES / "year_candidates.csv")
    cand = cand[(cand.option != "P") & (cand.pax_min_saved_per_day > 0)]
    cand = cand.sort_values("kpi_paxmin_per_keur", ascending=False).head(pool_size)
    recs = cand.to_dict("records")
    chosen, changes, path = [], [], []
    fitted = set()
    seed0 = 90_000
    base_res = run_many([("P0", "", per_month, seed0)], procs, ckpt="greedy")["P0"]
    prev = np.array([x["pax_delay_min"] for x in base_res["runs"]])
    prev_punct = float(np.mean([x["pax_punct"] for x in base_res["runs"]]))
    prev_fv = float(np.mean([x["punct_FV"] for x in base_res["runs"]]))
    path.append({"step": 0, "id": "", "name": "heute", "annual_cost_meur": 0.0, "capex_meur": 0.0,
                 "pax_delay_min_per_day": prev.mean(), "pax_punct": prev_punct, "punct_FV": prev_fv})
    cum_cost = cum_capex = 0.0
    for step in range(1, steps + 1):
        pool = [c for c in recs if c["id"] not in {x["id"] for x in chosen}
                and not any(_conflicts(c, x) for x in chosen)]
        if not pool:
            break
        tasks = [(c["id"], ";".join(changes + [c["changes"]]), per_month, seed0) for c in pool]
        res = run_many(tasks, procs, ckpt="greedy")
        best, best_kpi = None, -math.inf
        for c in pool:
            r = np.array([x["pax_delay_min"] for x in res[c["id"]]["runs"]])
            saved = (prev - r).mean()
            cost = c["annual_cost_meur"]
            if c["option"] == "E2" and isinstance(c.get("fit_parts"), str):
                # retrofit already paid by an earlier step is not charged again
                cost -= sum(v for g, v in json.loads(c["fit_parts"]).items() if g in fitted)
            if c["option"] == "FIT" and c.get("group") in fitted:
                continue
            kpi = saved * DAY_FACTOR / max(cost * 1e6, 1.0) * 1000 if cost > 0 else (math.inf if saved > 0 else -1)
            c["_m"] = (saved, kpi, r)
            c["_cost"] = cost
            if kpi > best_kpi:
                best, best_kpi = c, kpi
        if best is None or best["_m"][0] <= 0:
            break
        saved, kpi, r = best["_m"]
        chosen.append(best)
        changes.append(best["changes"])
        for op in best["changes"].split(";"):
            if op.startswith("fit:"):
                fitted.add(op[4:])
        cum_cost += best["_cost"]
        cum_capex += best["capex_meur"]
        runs = res[best["id"]]["runs"]
        prev = r
        path.append({"step": step, "id": best["id"], "name": best["name"], "option": best["option"],
                     "marginal_pax_min_per_day": saved, "marginal_kpi": kpi,
                     "annual_cost_meur": cum_cost, "capex_meur": cum_capex,
                     "pax_delay_min_per_day": r.mean(),
                     "pax_punct": float(np.mean([x["pax_punct"] for x in runs])),
                     "punct_FV": float(np.mean([x["punct_FV"] for x in runs]))})
        print(f"step {step}: {best['name']} (marginal {kpi:,.0f} pax-min/EUR 1,000)", flush=True)
        pd.DataFrame(path).to_csv(RES / "year_portfolio.csv", index=False)
    return pd.DataFrame(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["shifts", "screen", "refine", "greedy"])
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--steps", type=int, default=12)
    a = ap.parse_args()
    if a.stage == "shifts":
        prepare_shifts()
    elif a.stage == "screen":
        df = screen(a.procs)
    elif a.stage == "refine":
        df = refine(a.procs)
    else:
        df = greedy(a.procs, a.steps)
    pd.set_option("display.width", 250)
    if a.stage in ("screen", "refine"):
        print(df[["id", "name", "capex_meur", "annual_cost_meur", "pax_min_saved_per_year_m",
                  "kpi_paxmin_per_keur", "kpi_lo", "kpi_hi"]].head(30).to_string(index=False))


if __name__ == "__main__":
    main()
