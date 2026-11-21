"""Validate the simulated baseline against observed delays (DB data).

Compares punctuality (< 6 min) per category nationally and per model node for
long-distance (FV) and regional (NV) trains. Writes results/validation_nodes.csv
and results/validation.png.

    python -m golden_rails.validate --days 30
"""
from __future__ import annotations

import argparse
import json
from multiprocessing import Pool
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .network import load_network, place_name
from .sim import DisruptionModel, build_trains, plan_timetable, simulate_day

ROOT = Path(__file__).resolve().parent.parent
OBS = ROOT / "data" / "observed"
RES = ROOT / "results"

_NET = _TR = None


def _init():
    global _NET, _TR
    _NET = load_network()
    _TR = build_trains(_NET)
    plan_timetable(_NET, _TR)


def _day(seed):
    r = simulate_day(_NET, _TR, DisruptionModel(), seed)
    return {k: r[k] for k in ("punct_FV", "punct_NV", "punct_S", "punct_GV", "pax_punct")}, r["node_stats"]


def run(days=30, procs=4):
    with Pool(procs, initializer=_init) as pool:
        out = pool.map(_day, range(5000, 5000 + days))
    nat = pd.DataFrame([o[0] for o in out]).mean()
    agg: dict = {}
    for _, ns in out:
        for k, (n, ok, dsum) in ns.items():
            a = agg.setdefault(k, [0, 0, 0.0])
            a[0] += n
            a[1] += ok
            a[2] += dsum
    sim = pd.DataFrame([{"node": k[0], "cat": k[1], "sim_stops": v[0] / days, "sim_punct": v[1] / v[0],
                         "sim_mean_delay": v[2] / v[0]} for k, v in agg.items()])
    obs = pd.read_csv(OBS / "punctuality_by_node.csv").rename(
        columns={"punct_lt6": "obs_punct", "mean_delay": "obs_mean_delay", "stops": "obs_stops"})
    m = sim.merge(obs, on=["node", "cat"], how="inner")
    m = m[(m.obs_stops >= 300) & (m.sim_stops >= 5)]
    m.to_csv(RES / "validation_nodes.csv", index=False)
    obs_nat = pd.read_csv(OBS / "punctuality_by_category.csv").set_index("cat").punct_lt6
    summary = {"national_sim": {k: float(nat[f"punct_{k}"]) for k in ("FV", "NV", "S")},
               "national_obs": {k: float(obs_nat[k]) for k in ("FV", "NV", "S")}, "days": days}
    for cat in ("FV", "NV"):
        g = m[m.cat == cat]
        summary[f"nodes_{cat}"] = {"n": int(len(g)), "corr": float(np.corrcoef(g.sim_punct, g.obs_punct)[0, 1]),
                                   "mae_pp": float((g.sim_punct - g.obs_punct).abs().mean() * 100)}
    (RES / "validation.json").write_text(json.dumps(summary, indent=2))
    plot(m, summary)
    return summary


def plot(m, summary, path=RES / "validation.png"):
    from .plots import BLUE, GRID, INK, INK2, ORANGE, SURF
    net = load_network()
    fig, axes = plt.subplots(1, 2, figsize=(11, 5.2), dpi=150, facecolor=SURF)
    for ax, cat, col, title in ((axes[0], "FV", BLUE, "Fernverkehr (ICE/IC/EC)"),
                                (axes[1], "NV", ORANGE, "Regionalverkehr (RE/RB)")):
        g = m[m.cat == cat]
        ax.set_facecolor(SURF)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        ax.spines["left"].set_color(GRID)
        ax.spines["bottom"].set_color(GRID)
        ax.tick_params(colors=INK2, labelsize=8)
        lo = min(g.obs_punct.min(), g.sim_punct.min()) * 100 - 5
        ax.plot([lo, 100], [lo, 100], color=INK2, lw=1, ls="--", zorder=1)
        ax.scatter(g.obs_punct * 100, g.sim_punct * 100, s=28, color=col, edgecolors=SURF, linewidths=1, zorder=3)
        worst = g.assign(err=(g.sim_punct - g.obs_punct).abs()).sort_values("err").tail(5)
        for r in worst.itertuples():
            ax.annotate(place_name(net.nodes, r.node), (r.obs_punct * 100, r.sim_punct * 100), xytext=(4, 3),
                        textcoords="offset points", fontsize=7, color=INK2)
        s = summary[f"nodes_{cat}"]
        ax.set_title(f"{title}: r = {s['corr']:.2f}, mittl. Abweichung {s['mae_pp']:.1f} Pkt.",
                     loc="left", color=INK, fontsize=9.5)
        ax.set_xlabel("gemessen (DB-Daten Okt. 2026), % pünktlich", color=INK2, fontsize=8)
        ax.set_ylabel("simuliert, % pünktlich", color=INK2, fontsize=8)
        ax.set_xlim(lo, 100)
        ax.set_ylim(lo, 100)
        ax.grid(color=GRID, lw=0.6)
    fig.suptitle("Validierung je Knotenbahnhof: Simulation gegen gemessene Pünktlichkeit", x=0.01, ha="left",
                 color=INK, fontsize=11)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURF)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--procs", type=int, default=4)
    a = ap.parse_args()
    print(json.dumps(run(a.days, a.procs), indent=2))


if __name__ == "__main__":
    main()
