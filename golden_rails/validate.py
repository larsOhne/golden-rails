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

_NET = _TR = _DM = None


def _init(factors=None, seg_extra=None):
    global _NET, _TR, _DM
    _NET = load_network()
    _TR = build_trains(_NET)
    plan_timetable(_NET, _TR)
    _DM = DisruptionModel()
    if factors is not None:
        _DM.node_factor = dict(factors)
    if seg_extra is not None:
        _DM.seg_extra = dict(seg_extra)


def _day(seed):
    r = simulate_day(_NET, _TR, _DM, seed)
    return ({k: r[k] for k in ("punct_FV", "punct_NV", "punct_S", "punct_GV", "pax_punct")},
            r["node_stats"], r["leg_stats"])


def simulate_nodes(days, procs, factors=None, seed0=5000, seg_extra=None):
    with Pool(procs, initializer=_init, initargs=(factors, seg_extra)) as pool:
        out = pool.map(_day, range(seed0, seed0 + days))
    agg: dict = {}
    for _, ns, _ in out:
        for k, (n, ok, dsum) in ns.items():
            a = agg.setdefault(k, [0, 0, 0.0])
            a[0] += n
            a[1] += ok
            a[2] += dsum
    sim = pd.DataFrame([{"node": k[0], "cat": k[1], "sim_stops": v[0] / days, "sim_punct": v[1] / v[0],
                         "sim_mean_delay": v[2] / v[0]} for k, v in agg.items()])
    return out, sim


def leg_table(out) -> pd.DataFrame:
    agg: dict = {}
    for *_, ls in out:
        for k, (n, ssum) in ls.items():
            a = agg.setdefault(k, [0, 0.0])
            a[0] += n
            a[1] += ssum
    return pd.DataFrame([{"node": k[0], "next_node": k[1], "cat": k[2], "sim_n": v[0],
                          "sim_mean_change": v[1] / v[0]} for k, v in agg.items()])


SEG_EXTRA_SHRINK = 0.6


def calibrate_segments(iterations=4, days=16, procs=4, gain=0.8, obs_dir=OBS, min_trains=150):
    """Corridor extra delay so that simulated FV delay growth between consecutive stations
    matches the observed one. Only the residual not explained by modelled conflicts is added.
    Writes data/segment_delay_calibration.csv."""
    from .sim import SEG_EXTRA
    net = load_network()
    obs = pd.read_csv(obs_dir / "delay_change_by_leg.csv")
    obs = obs[(obs.cat == "FV") & (obs.trains >= min_trains) & (obs.node != obs.next_node)]
    paths = {}
    for r in obs.itertuples():
        try:
            p = net.shortest_path(r.node, r.next_node, "FV")
        except ValueError:
            continue
        if p:
            paths[(r.node, r.next_node)] = [s for s, _ in p]
    extra = {sid: 0.0 for sid in net.segments}
    for it in range(iterations):
        out, _ = simulate_nodes(days, procs, seed0=9000 + 100 * it, seg_extra=extra)
        sim = leg_table(out)
        m = obs.merge(sim[sim.cat == "FV"], on=["node", "next_node"], how="inner")
        num, den = {}, {}
        errs = []
        for r in m.itertuples():
            p = paths.get((r.node, r.next_node))
            if not p:
                continue
            res = r.mean_change - r.sim_mean_change
            errs.append(abs(res))
            L = sum(net.segments[s].length for s in p)
            for sid in p:
                share = net.segments[sid].length / L
                num[sid] = num.get(sid, 0.0) + r.trains * res * share
                den[sid] = den.get(sid, 0.0) + r.trains
        for sid in num:
            extra[sid] = max(0.0, extra[sid] + gain * num[sid] / den[sid])
        print(f"segments iteration {it + 1}: mean |leg error| {np.mean(errs):.2f} min over {len(errs)} legs",
              flush=True)
    # the fit only adds delay where the model under-predicts; where it over-predicts (too many
    # conflicts) it cannot subtract, so the national level ends up too pessimistic. A global
    # shrink of 0.6 balances spatial fit and national punctuality (see README, validation).
    pd.DataFrame([{"segment": k, "extra_min": round(v * SEG_EXTRA_SHRINK, 3)} for k, v in extra.items()]).to_csv(
        SEG_EXTRA, index=False)
    return extra


def calibrate_nodes(iterations=6, days=16, procs=4, damping=0.8, obs_dir=OBS):
    """Fit per-(node, type) dwell-extension factors so simulated punctuality per station
    matches the observed one. Writes data/node_delay_calibration.csv."""
    from .sim import NODE_FACTORS
    obs = pd.read_csv(obs_dir / "punctuality_by_node.csv")
    obs = obs[(obs.stops >= 300) & obs.cat.isin(["FV", "NV", "S"])]
    factors = {(r.node, r.cat): 1.0 for r in obs.itertuples()}
    hist = []
    for it in range(iterations):
        _, sim = simulate_nodes(days, procs, factors, seed0=7000 + 100 * it)
        m = sim.merge(obs, on=["node", "cat"])
        m = m[m.sim_stops >= 5]
        err = []
        for r in m.itertuples():
            lo, ls = max(1 - r.punct_lt6, 0.01), max(1 - r.sim_punct, 0.01)
            k = factors[(r.node, r.cat)] * (lo / ls) ** damping
            factors[(r.node, r.cat)] = float(np.clip(k, 0.0, 12.0))
            err.append(abs(r.sim_punct - r.punct_lt6))
        hist.append(float(np.mean(err)) * 100)
        print(f"iteration {it + 1}: mean |error| {hist[-1]:.1f} pp over {len(m)} station/type pairs", flush=True)
    pd.DataFrame([{"node": k[0], "cat": k[1], "factor": round(v, 3)} for k, v in factors.items()]).to_csv(
        NODE_FACTORS, index=False)
    return hist


def run(days=30, procs=4, obs_dir=OBS, tag=""):
    out, _ = simulate_nodes(days, procs)
    nat = pd.DataFrame([o[0] for o in out]).mean()
    legs = leg_table(out)
    agg: dict = {}
    for _, ns, _ in out:
        for k, (n, ok, dsum) in ns.items():
            a = agg.setdefault(k, [0, 0, 0.0])
            a[0] += n
            a[1] += ok
            a[2] += dsum
    sim = pd.DataFrame([{"node": k[0], "cat": k[1], "sim_stops": v[0] / days, "sim_punct": v[1] / v[0],
                         "sim_mean_delay": v[2] / v[0]} for k, v in agg.items()])
    obs = pd.read_csv(obs_dir / "punctuality_by_node.csv").rename(
        columns={"punct_lt6": "obs_punct", "mean_delay": "obs_mean_delay", "stops": "obs_stops"})
    m = sim.merge(obs, on=["node", "cat"], how="inner")
    m = m[(m.obs_stops >= 300) & (m.sim_stops >= 5)]
    m.to_csv(RES / f"validation_nodes{tag}.csv", index=False)
    obs_nat = pd.read_csv(obs_dir / "punctuality_by_category.csv").set_index("cat").punct_lt6
    summary = {"national_sim": {k: float(nat[f"punct_{k}"]) for k in ("FV", "NV", "S")},
               "national_obs": {k: float(obs_nat[k]) for k in ("FV", "NV", "S")}, "days": days}
    for cat in ("FV", "NV"):
        g = m[m.cat == cat]
        summary[f"nodes_{cat}"] = {"n": int(len(g)), "corr": float(np.corrcoef(g.sim_punct, g.obs_punct)[0, 1]),
                                   "mae_pp": float((g.sim_punct - g.obs_punct).abs().mean() * 100)}
    obs_legs = pd.read_csv(obs_dir / "delay_change_by_leg.csv")
    obs_legs = obs_legs[(obs_legs.cat == "FV") & (obs_legs.trains >= 150) & (obs_legs.node != obs_legs.next_node)]
    lm = obs_legs.merge(legs[legs.cat == "FV"], on=["node", "next_node"])
    lm.to_csv(RES / f"validation_legs{tag}.csv", index=False)
    summary["legs_FV"] = {"n": int(len(lm)), "corr": float(np.corrcoef(lm.mean_change, lm.sim_mean_change)[0, 1]),
                          "mae_min": float((lm.mean_change - lm.sim_mean_change).abs().mean())}
    summary["observed_month"] = json.loads((obs_dir / "meta.json").read_text())["month"]
    (RES / f"validation{tag}.json").write_text(json.dumps(summary, indent=2))
    plot(m, summary, RES / f"validation{tag}.png")
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
        ax.set_xlabel(f"gemessen (DB-Daten {summary.get('observed_month', '')}), % pünktlich", color=INK2, fontsize=8)
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
    ap.add_argument("--calibrate", action="store_true", help="fit per-station factors first")
    ap.add_argument("--obs", default=str(OBS), help="observed data directory (e.g. data/observed_2026-09)")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    if a.calibrate:
        calibrate_segments(procs=a.procs)
        calibrate_nodes(procs=a.procs)
    print(json.dumps(run(a.days, a.procs, Path(a.obs), a.tag), indent=2))


if __name__ == "__main__":
    main()
