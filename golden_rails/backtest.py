"""Backtest of the capacity response: the full closure Hamburg–Berlin (general renovation,
Aug 2025 – Apr 2026).

During the closure, long-distance trains Hamburg–Berlin ran via Uelzen and Stendal. The model
simulates the current timetable with the line Hamburg–Ludwigslust–Spandau removed: long-distance
trains take the detour via Uelzen–Stendal (added for this test), regional trains on the closed line
are dropped (replaced by buses in reality).

The simulated change of punctuality per station (closure minus normal operation) is compared with
the observed change between February 2026 (closure) and October 2026 (normal operation), each
corrected by the national change of that train category (difference in differences), which removes
part of the seasonal effect.

    python -m golden_rails.backtest --days 40
"""
from __future__ import annotations

import argparse
import json
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

from . import validate as V
from .network import Segment, load_network
from .sim import DisruptionModel, build_trains, plan_timetable, simulate_day

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "results"
CLOSED = ("HH-LUD", "LUD-SPA")
AFFECTED = ("HH", "HAR", "LG", "UE", "CE", "H", "SDL", "SPA", "B", "WOB", "BS", "MD", "HB", "SDL", "WB")

_NET = _TR = _DM = None


def closure_network():
    net = load_network()
    for sid in CLOSED:
        net.segments.pop(sid)
    # detour line Uelzen–Stendal (Amerikalinie), 107 km, mostly double track, 160 km/h
    net.segments["UE-SDL"] = Segment("UE-SDL", "UE", "SDL", 107.0, 2, 160.0, frozenset({"FV", "NV", "GV"}),
                                     1.0, terrain="flat", open_share=0.85, protected_share=0.15,
                                     note="detour during the Hamburg–Berlin closure")
    net.rebuild()
    return net


def _init(closed):
    global _NET, _TR, _DM
    _NET = closure_network() if closed else load_network()
    _TR = build_trains(_NET)
    plan_timetable(_NET, _TR)
    _DM = DisruptionModel()


def _day(seed):
    r = simulate_day(_NET, _TR, _DM, seed)
    return {k: r[k] for k in ("punct_FV", "punct_NV", "node_stats", "leg_stats")}


def simulate(closed, days, procs, seed0=5000):
    with Pool(procs, initializer=_init, initargs=(closed,)) as pool:
        return pool.map(_day, range(seed0, seed0 + days))


def run(days=40, procs=4):
    base = simulate(False, days, procs)
    clos = simulate(True, days, procs)
    nb, nc = V.node_table(base, days), V.node_table(clos, days)
    sim = nb.merge(nc, on=["node", "cat"], suffixes=("_base", "_closed"))
    nat = {c: np.mean([o[f"punct_{c}"] for o in clos]) - np.mean([o[f"punct_{c}"] for o in base])
           for c in ("FV", "NV")}
    sim["sim_change"] = sim.sim_punct_closed - sim.sim_punct_base
    o_feb = pd.read_csv(ROOT / "data/observed_2026-02/punctuality_by_node.csv")
    o_oct = pd.read_csv(ROOT / "data/observed/punctuality_by_node.csv")
    obs = o_feb.merge(o_oct, on=["node", "cat"], suffixes=("_feb", "_oct"))
    obs = obs[(obs.stops_feb >= 200) & (obs.stops_oct >= 200)]
    nat_o = {}
    for c in ("FV", "NV"):
        f = pd.read_csv(ROOT / "data/observed_2026-02/punctuality_by_category.csv").set_index("cat").punct_lt6[c]
        o = pd.read_csv(ROOT / "data/observed/punctuality_by_category.csv").set_index("cat").punct_lt6[c]
        nat_o[c] = f - o
    obs["obs_change"] = obs.punct_lt6_feb - obs.punct_lt6_oct
    m = sim.merge(obs[["node", "cat", "obs_change", "stops_feb"]], on=["node", "cat"])
    m = m[m.cat.isin(["FV", "NV"])]
    # difference in differences: station change relative to the national change of the category
    m["sim_did"] = m.sim_change - m.cat.map(nat)
    m["obs_did"] = m.obs_change - m.cat.map(nat_o)
    m["affected"] = m.node.isin(AFFECTED)
    m.to_csv(RES / "backtest_closure.csv", index=False)
    out = {"days": days, "national_change_sim": nat, "national_change_obs": nat_o}
    for c in ("FV", "NV"):
        g = m[m.cat == c]
        a = g[g.affected]
        out[c] = {"n_stations": int(len(g)), "corr_all": float(np.corrcoef(g.sim_did, g.obs_did)[0, 1]),
                  "n_affected": int(len(a)),
                  "corr_affected": float(np.corrcoef(a.sim_did, a.obs_did)[0, 1]) if len(a) > 2 else None,
                  "sign_agreement_affected": float((np.sign(a.sim_did) == np.sign(a.obs_did)).mean())
                  if len(a) else None,
                  "mean_sim_did_affected_pp": float(a.sim_did.mean() * 100),
                  "mean_obs_did_affected_pp": float(a.obs_did.mean() * 100)}
    (RES / "backtest_closure.json").write_text(json.dumps(out, indent=2))
    return out, m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--procs", type=int, default=4)
    a = ap.parse_args()
    out, m = run(a.days, a.procs)
    print(json.dumps(out, indent=2))
    pd.set_option("display.width", 200)
    print(m[m.affected][["node", "cat", "sim_did", "obs_did"]].round(3).to_string(index=False))


if __name__ == "__main__":
    main()
