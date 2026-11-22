"""Catalogue of measures with costs (EUR m) and annual cost.

Measure types (``option`` code):
  E     ETCS L2 on a corridor alongside the lineside signals (mixed traffic possible)
  E2    ETCS only (signals removed): includes the retrofit of every fleet group running there
  FIT   ETCS retrofit of one fleet group
  RES   hot reserve train sets of a fleet group at a node (+1 / +2)
  T1/T2 one / two more tracks in a corridor (built with ETCS)
  PLAT  one more platform track in a major node
  THR   one more route group in a node throat (flyover / extra approach track)
  NE    ETCS in a node (shorter route occupation for equipped trains)
  TT    timetable softening (shifts of +-3 min, see timetable_opt)
  P     planned projects (BVWP / Deutschlandtakt, published capex)

Annual cost = capex x annuity factor of the asset's lifetime (1.7% discount rate) + opex.
All unit costs are planning assumptions with ranges (``RANGE``); the sensitivity analysis uses them.
"""
from __future__ import annotations

import json

import pandas as pd

from . import fleet
from .costs import track_addition_cost
from .network import DATA, load_network, place_name, segment_name

DISCOUNT = 0.017
LIFE = {"infra": 60, "etcs": 25, "vehicle": fleet.VEHICLE_LIFE_YEARS, "onboard": fleet.ETCS_ONBOARD_LIFE_YEARS}
ETCS_MEUR_TKM = 1.0              # ETCS L2 + share of digital interlocking, per track-km
ETCS_ONLY_MEUR_TKM = 0.8         # without renewing lineside signals
SIGNAL_OPEX_SAVED_EUR_TKM = 15_000   # maintenance of lineside signals saved per track-km and year (ETCS only)
ETCS_OPEX_EUR_TKM = 10_000       # maintenance of ETCS equipment per track-km and year
TRACK_OPEX_EUR_TKM = 130_000
PLATFORM_MEUR = 150.0            # one platform track in a major urban node (80-250)
THROAT_MEUR = 250.0              # flyover / additional approach track (150-400)
NODE_ETCS_MEUR = 40.0            # ETCS in a node: fixed part ...
NODE_ETCS_PER_PLATFORM = 3.0     # ... plus per platform track
NODE_OPEX_EUR = 1_000_000        # upkeep of an added platform / flyover per year
RANGE = {"low": 0.6, "high": 1.6}   # cost multipliers for the sensitivity analysis


def annuity(n, r=DISCOUNT):
    return r / (1 - (1 + r) ** -n)


def _row(code, mid, name, changes, capex, annual, kind="measure", **kw):
    return {"id": f"{code}-{mid}", "option": code, "name": name, "changes": changes, "capex_meur": capex,
            "annual_cost_meur": annual, "kind": kind, **kw}


def vehicle_price(group):
    cat = fleet.group_category(group)
    if group == "Fernverkehr ICE":
        return fleet.VEHICLE_PRICE_MEUR["FV-ICE"]
    return fleet.VEHICLE_PRICE_MEUR.get(cat, 10.0)


def fit_cost(group, sizes):
    n = sizes.get(group, 0)
    capex = n * fleet.FIT_MEUR_PER_VEHICLE.get(fleet.group_category(group), 0.5)
    return capex, capex * annuity(LIFE["onboard"])


def catalogue(trains, net=None, reserve_top=40):
    """All candidate measures. ``trains``: the trains of a representative (normal) month."""
    net = net or load_network()
    sizes = fleet.fleet_sizes(trains)
    rows = []
    users = {}                       # segment -> fleet groups running on it
    for tr in trains:
        for st in tr.steps:
            users.setdefault(st[0], set()).add(tr.group)
    for s in net.segments.values():
        nm = segment_name(net, s.id)
        tkm = s.length * s.tracks
        if not s.etcs:
            capex = tkm * ETCS_MEUR_TKM
            rows.append(_row("E", s.id, f"ETCS {nm}", f"etcs:{s.id}", capex,
                             capex * annuity(LIFE["etcs"]) + tkm * ETCS_OPEX_EUR_TKM / 1e6, segment=s.id))
            groups = sorted(g for g in users.get(s.id, ()) if g)
            fcap = sum(fit_cost(g, sizes)[0] for g in groups)
            fann = sum(fit_cost(g, sizes)[1] for g in groups)
            capex2 = tkm * ETCS_ONLY_MEUR_TKM
            ann2 = capex2 * annuity(LIFE["etcs"]) + tkm * (ETCS_OPEX_EUR_TKM - SIGNAL_OPEX_SAVED_EUR_TKM) / 1e6
            rows.append(_row("E2", s.id, f"Nur-ETCS {nm} (inkl. Nachrüstung {len(groups)} Flotten)",
                             ";".join([f"etcs2:{s.id}"] + [f"fit:{g}" for g in groups]), capex2 + fcap, ann2 + fann,
                             segment=s.id, fitted_groups="|".join(groups),
                             fit_parts=json.dumps({g: fit_cost(g, sizes)[1] for g in groups})))
        for k, code in ((1, "T1"), (2, "T2")):
            c = track_addition_cost(s, k)
            capex = c["total"]
            ann = (c["construction"] + c["land"]) * annuity(LIFE["infra"]) + c["etcs"] * annuity(LIFE["etcs"]) \
                + c["track_km"] * (TRACK_OPEX_EUR_TKM + ETCS_OPEX_EUR_TKM) / 1e6
            rows.append(_row(code, s.id, f"+{k} Gleis{'e' if k > 1 else ''} {nm} ({s.tracks}→{s.tracks + k})",
                             f"tracks:{s.id}:{s.tracks + k}", capex, ann, segment=s.id,
                             capex_construction=c["construction"], capex_land=c["land"], capex_etcs=c["etcs"]))
    for g, n in sizes.items():
        if g == "Güterverkehr" or n <= 0:
            pass
        capex, ann = fit_cost(g, sizes)
        if capex > 0:
            rows.append(_row("FIT", g.replace(" ", "_"), f"ETCS-Nachrüstung {g} ({n} Fahrzeuge)", f"fit:{g}",
                             capex, ann, group=g))
    origins = {}
    for tr in trains:
        if tr.ttype != "GV":
            origins[(tr.stops[0], tr.group)] = origins.get((tr.stops[0], tr.group), 0.0) + tr.pax
    for (node, g), _ in sorted(origins.items(), key=lambda kv: -kv[1])[:reserve_top]:
        price = vehicle_price(g)
        for k in (1, 2):
            capex = k * price
            ann = capex * annuity(LIFE["vehicle"]) + k * fleet.RESERVE_STANDBY_MEUR_YR
            rows.append(_row("RES", f"{node}_{g.replace(' ', '_')}_{k}",
                             f"{k} Reservezug{'e' if k > 1 else ''} {g} in {place_name(net.nodes, node)}",
                             f"reserve:{node}:{g}:{k}", capex, ann, node=node, group=g))
    for node, cap in net.node_cap.items():
        pn = place_name(net.nodes, node)
        rows.append(_row("PLAT", node, f"+1 Bahnsteiggleis {pn}", f"plat:{node}:1", PLATFORM_MEUR,
                         PLATFORM_MEUR * annuity(LIFE["infra"]) + NODE_OPEX_EUR / 1e6, node=node))
        rows.append(_row("THR", node, f"Überwerfung / Einfahrt {pn}", f"throat:{node}:1", THROAT_MEUR,
                         THROAT_MEUR * annuity(LIFE["infra"]) + NODE_OPEX_EUR / 1e6, node=node))
        c = NODE_ETCS_MEUR + NODE_ETCS_PER_PLATFORM * cap.platforms
        rows.append(_row("NE", node, f"ETCS im Knoten {pn}", f"netcs:{node}", c, c * annuity(LIFE["etcs"]),
                         node=node))
    rows.append(_row("TT", "soft", "Fahrplan aufweichen (±3 min, gleiche Ankünfte je Stunde)", "tt:soft", 0.0, 0.0))
    planned = pd.read_csv(DATA / "candidates.csv")
    for r in planned.itertuples():
        rows.append(_row("P", r.id, r.name, r.changes, float(r.capex_meur),
                         float(r.capex_meur) * annuity(LIFE["infra"]), kind="planned project"))
    return pd.DataFrame(rows)


def cost_scaled(df, which):
    """Annual cost with all unit costs at the low or high end of their range."""
    return df.annual_cost_meur * RANGE[which]
