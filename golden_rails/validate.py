"""Calibrate and validate the simulated baseline against observed delays (DB data).

Calibration (``--calibrate``), in this order:
  1. node route occupation TAU (grid) against the observed delay gained while standing in major nodes;
  2. share of entry delay of trains whose vehicle comes from a modelled predecessor (circulations)
     against the observed share of trains entering the model late;
  3. vehicle failure rates against the observed cancellations attributed to vehicles
     (``ALPHA_VEHICLE`` of all cancelled stops; full cancellations -> failure before departure,
     cancellations from a station on -> failure en route);
  4. corridor correction (both signs) against the observed delay growth between consecutive stations;
  5. dwell factors per station and type against observed station punctuality.

Validation compares punctuality (< 6 min) per category nationally and per model node, delay growth
between consecutive stations, delay gained in nodes, cancellations and delay propagation in
turnarounds. Writes results/validation*.{csv,json,png}.

    python -m golden_rails.validate --calibrate --days 40
    python -m golden_rails.validate --days 40 --obs data/observed_2026-09 --tag _sep
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

from . import sim as S
from .network import load_network, place_name
from .sim import DisruptionModel, build_trains, plan_timetable, simulate_day

ROOT = Path(__file__).resolve().parent.parent
OBS = ROOT / "data" / "observed"
RES = ROOT / "results"
ALPHA_VEHICLE = 0.4      # share of all cancellations attributed to vehicle failures (assumption)
SEG_EXTRA_SHRINK = 0.6
MAX_SPEEDUP = 0.15       # negative corridor correction: at most 15% faster than modelled

_NET = _TR = _DM = None


def _init(params):
    global _NET, _TR, _DM
    params = params or {}
    if "tau" in params:
        S.TAU = params["tau"]
    _NET = load_network()
    _TR = build_trains(_NET)
    plan_timetable(_NET, _TR)
    _DM = DisruptionModel()
    if params.get("factors") is not None:
        _DM.node_factor = dict(params["factors"])
    if params.get("seg_extra") is not None:
        _DM.seg_extra = dict(params["seg_extra"])
    for k in ("p_pred_scale", "p_fail_pre", "fail_per_km", "turn_recovery"):
        if params.get(k) is not None:
            setattr(_DM, k, dict(params[k]))


KEEP = ("punct_FV", "punct_NV", "punct_S", "punct_GV", "pax_punct", "node_stats", "leg_stats", "starts",
        "cancel_visits", "visits", "turn_stats", "node_dwell", "cancel_share_FV", "cancel_share_NV",
        "cancel_share_S")


def _day(seed):
    r = simulate_day(_NET, _TR, _DM, seed)
    return {k: r[k] for k in KEEP}


def simulate(days, procs, params=None, seed0=5000):
    with Pool(procs, initializer=_init, initargs=(params,)) as pool:
        return pool.map(_day, range(seed0, seed0 + days))


# ---------------------------------------------------------------- aggregation
def _sum_dict(out, key, width):
    agg = {}
    for o in out:
        for k, v in o[key].items():
            a = agg.setdefault(k, [0.0] * width)
            vv = v if isinstance(v, (list, tuple)) else [v]
            for j in range(width):
                a[j] += vv[j]
    return agg


def node_table(out, days):
    agg = _sum_dict(out, "node_stats", 3)
    return pd.DataFrame([{"node": k[0], "cat": k[1], "sim_stops": v[0] / days, "sim_punct": v[1] / v[0],
                          "sim_mean_delay": v[2] / v[0]} for k, v in agg.items()])


def leg_table(out) -> pd.DataFrame:
    agg = _sum_dict(out, "leg_stats", 2)
    return pd.DataFrame([{"node": k[0], "next_node": k[1], "cat": k[2], "sim_n": v[0],
                          "sim_mean_change": v[1] / v[0]} for k, v in agg.items()])


def dwell_table(out) -> pd.DataFrame:
    agg = _sum_dict(out, "node_dwell", 2)
    return pd.DataFrame([{"node": k[0], "cat": k[1], "sim_n": v[0], "sim_dwell_gain": v[1] / v[0]}
                         for k, v in agg.items() if v[0] > 0])


def start_share(out) -> dict:
    agg = _sum_dict(out, "starts", 2)
    return {k: v[1] / max(1, v[0]) for k, v in agg.items()}


def cancel_shares(out) -> dict:
    cv = _sum_dict(out, "cancel_visits", 2)
    vi = _sum_dict(out, "visits", 1)
    return {k: (cv[k][0] / max(1, vi[k][0]), cv[k][1] / max(1, vi[k][0])) for k in ("FV", "NV", "S")}


def turn_table(out) -> pd.DataFrame:
    agg = _sum_dict(out, "turn_stats", 4)
    return pd.DataFrame([{"cat": k[0], "arr_bin": k[1], "sim_n": v[0], "sim_mean_dep": v[1] / max(1, v[0]),
                          "sim_p_dep_ge3": v[2] / max(1, v[0]), "sim_p_dep_ge6": v[3] / max(1, v[0])}
                         for k, v in agg.items()])


# ---------------------------------------------------------------- observed targets
def obs_entry_share(obs_dir=OBS) -> dict:
    h = pd.read_csv(obs_dir / "entry_delay_hist.csv")
    return {c: float(g[g.d >= 1].n.sum() / g.n.sum()) for c, g in h.groupby("cat")}


def obs_cancel_targets(obs_dir=OBS, alpha=ALPHA_VEHICLE) -> dict:
    c = pd.read_csv(obs_dir / "cancellations.csv").set_index("cat")
    return {k: (alpha * c.loc[k, "cancelled_stop_share"] * c.loc[k, "share_full"],
                alpha * c.loc[k, "cancelled_stop_share"] * c.loc[k, "share_tail"]) for k in ("FV", "NV", "S")}


def dwell_error(out, obs_dir=OBS):
    o = pd.read_csv(obs_dir / "node_dwell_gain.csv")
    m = o[o.stops >= 300].merge(dwell_table(out), on=["node", "cat"])
    m = m[m.sim_n >= 20]
    return float((m.mean_dwell_gain - m.sim_dwell_gain).abs().mean()), m


# ---------------------------------------------------------------- calibration steps
def write_ops(params):
    rows = [{"param": "tau", "cat": "all", "value": params["tau"]}]
    for k in ("p_pred_scale", "p_fail_pre", "fail_per_km", "turn_recovery"):
        rows += [{"param": k, "cat": c, "value": v} for c, v in params.get(k, {}).items()]
    pd.DataFrame(rows).to_csv(S.OPS_CALIB, index=False)


def calibrate_tau(params, procs, days=10, grid=(1.0, 1.5, 2.0, 2.5, 3.0)):
    res = []
    for tau in grid:
        out = simulate(days, procs, {**params, "tau": tau}, seed0=11000)
        err, _ = dwell_error(out)
        res.append((err, tau))
        print(f"tau {tau}: mean |node dwell gain error| {err:.2f} min", flush=True)
    params["tau"] = min(res)[1]
    return params


def calibrate_ops(params, procs, days=24, iterations=4):
    """Entry-delay share of trains with a predecessor, and vehicle failure rates."""
    # trains whose vehicle comes from a modelled turnaround: exogenous start delay calibrated on the
    # observed departures after an on-time arrival (turnaround.csv, inbound delay 0-5 min, >= 6 min late)
    ot = pd.read_csv(OBS / "turnaround.csv")
    t_turn = {r.cat: float(r.p_dep_ge6) for r in ot[ot.arr_bin == "0-5"].itertuples()}
    t_canc = obs_cancel_targets()
    for it in range(iterations):
        out = simulate(days, procs, params, seed0=12000 + 100 * it)
        tt = turn_table(out)
        st = {r.cat: r.sim_p_dep_ge6 for r in tt[tt.arr_bin == "0-5"].itertuples()}
        cs = cancel_shares(out)
        se = {c: st.get(c, 0.0) for c in ("FV", "NV", "S")}
        t_entry = t_turn
        ot_mid = ot[ot.arr_bin.isin(["5-10", "10-20", "20-40"])].set_index(["cat", "arr_bin"]).p_dep_ge6
        tt_mid = tt[tt.arr_bin.isin(["5-10", "10-20", "20-40"])].set_index(["cat", "arr_bin"]).sim_p_dep_ge6
        for c in ("FV", "NV", "S"):
            try:
                gap = float((tt_mid.loc[c] - ot_mid.loc[c]).mean())
            except KeyError:
                gap = 0.0
            # more propagation than observed -> more recovery (about 10 min per 0.25 probability)
            params["turn_recovery"][c] = float(np.clip(params["turn_recovery"][c] + 40.0 * gap, 0.0, 30.0))
        for c in ("FV", "NV", "S"):
            params["p_pred_scale"][c] = float(np.clip(
                params["p_pred_scale"][c] * (t_turn[c] / max(st.get(c, 1e-6), 1e-6)), 0.0, 3.0))
            params["p_fail_pre"][c] = float(np.clip(
                params["p_fail_pre"][c] * (t_canc[c][0] / max(cs[c][0], 1e-6)) ** 0.7, 0.0, 0.2))
            params["fail_per_km"][c] = float(np.clip(
                params["fail_per_km"][c] * (t_canc[c][1] / max(cs[c][1], 1e-6)) ** 0.7, 0.0, 1e-2))
        print(f"ops iteration {it + 1}: turnaround dep>=6 after on-time arrival sim/obs "
              + ", ".join(f"{c} {se.get(c, 0):.2f}/{t_entry[c]:.2f}" for c in ("FV", "NV", "S"))
              + "; recovery " + ", ".join(f"{c} {params['turn_recovery'][c]:.1f}" for c in ("FV", "NV", "S"))
              + "; cancellations full/tail sim vs obs "
              + ", ".join(f"{c} {cs[c][0] * 100:.2f}/{t_canc[c][0] * 100:.2f} {cs[c][1] * 100:.2f}/{t_canc[c][1] * 100:.2f}"
                          for c in ("FV", "NV", "S")), flush=True)
    return params


def calibrate_segments(params, procs, iterations=4, days=16, gain=0.8, obs_dir=OBS, min_trains=150):
    """Corridor correction (both signs) so that simulated FV delay growth between consecutive stations
    matches the observed one."""
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
    floor = {sid: -MAX_SPEEDUP * net.run_minutes(s, "FV") for sid, s in net.segments.items()}
    for it in range(iterations):
        out = simulate(days, procs, {**params, "seg_extra": extra}, seed0=9000 + 100 * it)
        sim = leg_table(out)
        m = obs.merge(sim[sim.cat == "FV"], on=["node", "next_node"], how="inner")
        num, den, errs = {}, {}, []
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
            extra[sid] = max(floor[sid], extra[sid] + gain * num[sid] / den[sid])
        print(f"segments iteration {it + 1}: mean |leg error| {np.mean(errs):.2f} min over {len(errs)} legs",
              flush=True)
    # positive part shrunk (the fit cannot see every conflict the model generates itself)
    final = {k: (v * SEG_EXTRA_SHRINK if v > 0 else v) for k, v in extra.items()}
    pd.DataFrame([{"segment": k, "extra_min": round(v, 3)} for k, v in final.items()]).to_csv(
        S.SEG_EXTRA, index=False)
    params["seg_extra"] = final
    return params


def calibrate_nodes(params, procs, iterations=5, days=16, damping=0.8, obs_dir=OBS):
    """Per-(node, type) dwell-extension factors so simulated station punctuality matches the observed."""
    obs = pd.read_csv(obs_dir / "punctuality_by_node.csv")
    obs = obs[(obs.stops >= 300) & obs.cat.isin(["FV", "NV", "S"])]
    factors = {(r.node, r.cat): 1.0 for r in obs.itertuples()}
    for it in range(iterations):
        out = simulate(days, procs, {**params, "factors": factors}, seed0=7000 + 100 * it)
        sim = node_table(out, days)
        m = sim.merge(obs, on=["node", "cat"])
        m = m[m.sim_stops >= 5]
        err = []
        for r in m.itertuples():
            lo, ls = max(1 - r.punct_lt6, 0.01), max(1 - r.sim_punct, 0.01)
            k = factors[(r.node, r.cat)] * (lo / ls) ** damping
            factors[(r.node, r.cat)] = float(np.clip(k, 0.0, 12.0))
            err.append(abs(r.sim_punct - r.punct_lt6))
        print(f"nodes iteration {it + 1}: mean |error| {np.mean(err) * 100:.1f} pp over {len(m)} station/type pairs",
              flush=True)
    pd.DataFrame([{"node": k[0], "cat": k[1], "factor": round(v, 3)} for k, v in factors.items()]).to_csv(
        S.NODE_FACTORS, index=False)
    params["factors"] = factors
    return params


def calibrate(procs=4):
    dm = DisruptionModel()
    params = {"tau": S.TAU, "factors": {}, "seg_extra": {},
              "p_pred_scale": dict(dm.p_pred_scale), "p_fail_pre": dict(dm.p_fail_pre),
              "fail_per_km": dict(dm.fail_per_km), "turn_recovery": dict(dm.turn_recovery)}
    params = calibrate_tau(params, procs, grid=(1.5, 2.0, 3.0))
    params = calibrate_ops(params, procs)
    write_ops(params)
    params = calibrate_segments(params, procs)
    params = calibrate_nodes(params, procs)
    params = calibrate_ops(params, procs, iterations=2)
    write_ops(params)
    return params



def recalibrate_ops(procs=4, iterations=4):
    """Re-run only the entry/failure calibration on top of the stored corridor and node calibration."""
    dm = DisruptionModel()
    params = {"tau": S.TAU, "p_pred_scale": dict(dm.p_pred_scale), "p_fail_pre": dict(dm.p_fail_pre),
              "fail_per_km": dict(dm.fail_per_km), "turn_recovery": dict(dm.turn_recovery)}
    params = calibrate_ops(params, procs, iterations=iterations)
    write_ops(params)
    return params


# ---------------------------------------------------------------- validation
def run(days=30, procs=4, obs_dir=OBS, tag=""):
    out = simulate(days, procs)
    nat = pd.DataFrame([{k: o[k] for k in ("punct_FV", "punct_NV", "punct_S", "pax_punct")} for o in out]).mean()
    sim = node_table(out, days)
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
                                   "mae_pp": float((g.sim_punct - g.obs_punct).abs().mean() * 100),
                                   "bias_pp": float((g.sim_punct - g.obs_punct).mean() * 100)}
    legs = leg_table(out)
    obs_legs = pd.read_csv(obs_dir / "delay_change_by_leg.csv")
    obs_legs = obs_legs[(obs_legs.cat == "FV") & (obs_legs.trains >= 150) & (obs_legs.node != obs_legs.next_node)]
    lm = obs_legs.merge(legs[legs.cat == "FV"], on=["node", "next_node"])
    lm.to_csv(RES / f"validation_legs{tag}.csv", index=False)
    summary["legs_FV"] = {"n": int(len(lm)), "corr": float(np.corrcoef(lm.mean_change, lm.sim_mean_change)[0, 1]),
                          "mae_min": float((lm.mean_change - lm.sim_mean_change).abs().mean())}
    err, dm_ = dwell_error(out, obs_dir)
    dm_.to_csv(RES / f"validation_node_dwell{tag}.csv", index=False)
    summary["node_dwell"] = {"n": int(len(dm_)), "mae_min": err,
                             "corr": float(np.corrcoef(dm_.mean_dwell_gain, dm_.sim_dwell_gain)[0, 1])}
    cs = cancel_shares(out)
    oc = pd.read_csv(obs_dir / "cancellations.csv").set_index("cat")
    summary["cancellations"] = {c: {"sim_share": cs[c][0] + cs[c][1],
                                    "obs_vehicle_share": ALPHA_VEHICLE * float(oc.loc[c, "cancelled_stop_share"]
                                                                               * (oc.loc[c, "share_full"] + oc.loc[c, "share_tail"])),
                                    "obs_all_share": float(oc.loc[c, "cancelled_stop_share"])} for c in ("FV", "NV", "S")}
    tt = turn_table(out)
    ot = pd.read_csv(obs_dir / "turnaround.csv")
    tm = ot.merge(tt, on=["cat", "arr_bin"], how="left")
    tm.to_csv(RES / f"validation_turnaround{tag}.csv", index=False)
    summary["turnaround"] = {c: {r.arr_bin: {"obs_p_dep_ge6": round(float(r.p_dep_ge6), 3),
                                             "sim_p_dep_ge6": round(float(r.sim_p_dep_ge6), 3) if r.sim_p_dep_ge6 == r.sim_p_dep_ge6 else None}
                                 for r in g.itertuples()} for c, g in tm.groupby("cat")}
    summary["entry_share"] = {"sim": start_share(out), "obs": obs_entry_share(obs_dir)}
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
    ap.add_argument("--calibrate", action="store_true", help="run the calibration first")
    ap.add_argument("--recalibrate-ops", action="store_true", help="only entry-delay and failure rates")
    ap.add_argument("--obs", default=str(OBS), help="observed data directory (e.g. data/observed_2026-09)")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    if a.calibrate:
        calibrate(a.procs)
    if a.recalibrate_ops:
        recalibrate_ops(a.procs)
    print(json.dumps(run(a.days, a.procs, Path(a.obs), a.tag), indent=2))


if __name__ == "__main__":
    main()

