"""What does it cost to reach a punctuality target (e.g. 95 %)?

1. Infrastructure cost curve: corridor options (ETCS, +1/+2 tracks) are added
   greedily in the order of their screening KPI (best value per euro first),
   cumulated into packages and each package is simulated. The last package is
   the "maximum" network: ETCS everywhere and two extra tracks on every corridor.
2. Operational levers: on the current and on the maximum network, the
   exogenous delays are scaled down (share of trains starting late / longer
   dwell times, and infrastructure incidents) to show what else is needed.

Punctuality = share of stops (or of passengers) arriving < 6 min late, as DB reports it.
"""
from __future__ import annotations

import argparse
import json
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

from . import evaluate as ev
from .costs import etcs_upgrade_cost, track_addition_cost
from .network import load_network, segment_name
from .sim import DisruptionModel, build_trains, plan_timetable, simulate_day

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "results"
BUDGETS_BN = [0.25, 0.5, 1, 2, 4, 8, 16, 32, 64, 128, 256]
METRICS = ["punct_FV", "punct_NV", "punct_S", "punct_GV", "pax_punct", "pax_punct_FV",
           "pax_delay_min", "avg_delay_per_pax"]


def _state_cost(seg, added, etcs):
    c = track_addition_cost(seg, added)["total"] if added else 0.0
    if etcs:
        c += etcs_upgrade_cost(seg)["total"]
    return c


def _changes(net, state):
    ops = []
    for sid, (added, etcs) in state.items():
        if added:
            ops.append(f"tracks:{sid}:{net.segments[sid].tracks + added}")
        if etcs and not net.segments[sid].etcs:
            ops.append(f"etcs:{sid}")
    return ";".join(ops)


def build_packages(screen: pd.DataFrame) -> list[dict]:
    net = load_network()
    opts = screen[(screen.kind == "corridor option") & (screen.kpi_paxmin_per_keur > 0)]
    opts = opts.sort_values("kpi_paxmin_per_keur", ascending=False)
    state: dict[str, tuple[int, bool]] = {}
    cost = 0.0
    added_tkm = 0.0
    steps = []
    for o in opts.itertuples():
        sid = o.segment
        seg = net.segments[sid]
        a, e = state.get(sid, (0, False))
        na, ne = a, e
        if o.option == "E":
            ne = True
        elif o.option == "T1":
            na = max(a, 1)
        elif o.option == "T1E":
            na, ne = max(a, 1), True
        elif o.option == "T2":
            na = 2
        if (na, ne) == (a, e):
            continue
        dc = _state_cost(seg, na, ne) - _state_cost(seg, a, e)
        cost += dc
        added_tkm += (na - a) * seg.length
        state[sid] = (na, ne)
        steps.append({"cost_meur": cost, "track_km": added_tkm, "state": dict(state),
                      "last": f"{o.option} {segment_name(net, sid)}"})
    packages = [{"name": "Heute (Basis)", "capex_meur": 0.0, "track_km": 0.0, "changes": "", "n_measures": 0}]
    for b in BUDGETS_BN:
        within = [s for s in steps if s["cost_meur"] <= b * 1000]
        if within and within[-1]["cost_meur"] > packages[-1]["capex_meur"]:
            s = within[-1]
            packages.append({"name": f"Paket bis €{b:g} Mrd.", "capex_meur": s["cost_meur"],
                             "track_km": s["track_km"], "changes": _changes(net, s["state"]),
                             "n_measures": len(within)})
    s = steps[-1]
    packages.append({"name": "Alle wirtschaftlichen Maßnahmen", "capex_meur": s["cost_meur"],
                     "track_km": s["track_km"], "changes": _changes(net, s["state"]),
                     "n_measures": len(steps)})
    full = {sid: (2, True) for sid in net.segments}
    packages.append({"name": "Maximum: ETCS + 2 Gleise überall",
                     "capex_meur": sum(_state_cost(net.segments[k], 2, True) for k in full),
                     "track_km": sum(2 * s.length for s in net.segments.values()),
                     "changes": _changes(net, full), "n_measures": len(full)})
    return packages


_DM_DICT = None


def _init(dm_dict):
    global _DM_DICT
    _DM_DICT = dm_dict


def _run(args):
    name, changes, seeds, levers = args
    dm = DisruptionModel(**_DM_DICT)
    f_train, f_inc = levers
    dm.p_origin = {k: v * f_train for k, v in dm.p_origin.items()}
    dm.p_dwell *= f_train
    dm.rate_track_fault *= f_inc
    dm.rate_full_closure *= f_inc
    net = load_network()
    if changes:
        net.apply_changes(changes)
    trains = build_trains(net)
    plan_timetable(net, trains, ev.base_plan())
    runs = [simulate_day(net, trains, dm, s) for s in seeds]
    out = {"name": name, "f_train": f_train, "f_incidents": f_inc}
    for m in METRICS:
        out[m] = float(np.mean([r[m] for r in runs]))
    return out


def run(seeds=30, procs=4, out_dir=RES):
    screen = pd.read_csv(out_dir / "screening.csv")
    packages = build_packages(screen)
    s = list(range(2000, 2000 + seeds))
    tasks = [(p["name"], p["changes"], s, (1.0, 1.0)) for p in packages]
    base, mx = packages[0], packages[-1]
    lever_grid = [(ft, fi) for ft in (1.0, 0.5, 0.25, 0.1, 0.0) for fi in (1.0, 0.5, 0.0) if (ft, fi) != (1.0, 1.0)]
    for p in (base, mx):
        tasks += [(p["name"], p["changes"], s, lv) for lv in lever_grid]
    # combinations: mid-size infrastructure packages with fewer exogenous train delays
    for p in packages:
        if p["name"] in ("Paket bis €8 Mrd.", "Paket bis €32 Mrd."):
            tasks += [(p["name"], p["changes"], s, (ft, 1.0)) for ft in (0.5, 0.25, 0.1)]
    with Pool(procs, initializer=_init, initargs=(DisruptionModel().to_dict(),)) as pool:
        res = pool.map(_run, tasks, chunksize=1)

    af = ev.annuity_factor()
    meta = {p["name"]: p for p in packages}
    rows = []
    for r in res:
        p = meta[r["name"]]
        rows.append({**r, "capex_meur": p["capex_meur"], "track_km": p["track_km"],
                     "n_measures": p["n_measures"],
                     "annual_cost_meur": p["capex_meur"] * af + p["track_km"] * ev.OPEX_EUR_PER_TRACK_KM / 1e6})
    df = pd.DataFrame(rows)
    curve = df[(df.f_train == 1.0) & (df.f_incidents == 1.0)].sort_values("capex_meur")
    levers = df[~((df.f_train == 1.0) & (df.f_incidents == 1.0))]
    levers = pd.concat([curve[curve.name.isin([base["name"], mx["name"]])], levers])
    curve.to_csv(out_dir / "punctuality_curve.csv", index=False)
    levers.sort_values(["capex_meur", "f_train", "f_incidents"], ascending=[True, False, False]).to_csv(
        out_dir / "punctuality_levers.csv", index=False)
    pd.DataFrame([{k: v for k, v in p.items() if k != "changes"} | {"changes": p["changes"]}
                  for p in packages]).to_csv(out_dir / "punctuality_packages.csv", index=False)
    return curve, levers


def plot(out_dir=RES):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from .plots import BLUE, GRID, INK, INK2, ORANGE, SURF, _style

    curve = pd.read_csv(out_dir / "punctuality_curve.csv")
    fig, ax = plt.subplots(figsize=(9, 5), dpi=150, facecolor=SURF)
    _style(ax)
    ax.grid(axis="y", color=GRID, lw=0.8)
    x = curve.capex_meur / 1000
    xs = np.maximum(x, 0.05)
    ax.plot(xs, curve.punct_FV * 100, color=BLUE, lw=2, marker="o", ms=8, mec=SURF, mew=2,
            label="Fernverkehr (ICE/IC), Anteil Halte < 6 min")
    ax.plot(xs, curve.pax_punct * 100, color=ORANGE, lw=2, marker="o", ms=8, mec=SURF, mew=2,
            label="Alle Fahrgäste (Fern- und Nahverkehr)")
    ax.axhline(95, color=INK2, lw=1, ls="--")
    ax.text(xs.min(), 95.6, "Ziel 95 %", color=INK2, fontsize=8)
    ax.set_xscale("log")
    ax.set_xlabel("Investition (Capex), Mrd. € – logarithmisch; ganz links = heute", color=INK2, fontsize=8)
    ax.set_ylabel("Pünktlichkeit, %", color=INK2, fontsize=8)
    ax.set_ylim(55, 100)
    ax.set_title("Nur Infrastruktur: 95 % der Fahrgäste erst ab ~250 Mrd. €, Fernverkehr nie",
                 loc="left", color=INK, fontsize=11)
    from matplotlib.ticker import FixedLocator, FuncFormatter
    ax.xaxis.set_major_locator(FixedLocator([0.1, 0.3, 1, 3, 10, 30, 100, 300]))
    ax.xaxis.set_minor_locator(FixedLocator([]))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}".replace(".", ",")))
    ax.legend(frameon=False, fontsize=8, loc="lower right", labelcolor=INK)
    fig.tight_layout()
    fig.savefig(out_dir / "punctuality_cost_curve.png", facecolor=SURF)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--procs", type=int, default=4)
    a = ap.parse_args()
    curve, levers = run(a.seeds, a.procs)
    plot()
    pd.set_option("display.width", 250)
    cols = ["name", "capex_meur", "annual_cost_meur", "punct_FV", "punct_NV", "punct_S", "punct_GV", "pax_punct"]
    print(curve[cols].round(3).to_string(index=False))
    print(levers[cols[:1] + ["f_train", "f_incidents"] + cols[3:]].round(3).to_string(index=False))


if __name__ == "__main__":
    main()
