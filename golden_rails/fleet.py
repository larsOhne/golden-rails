"""Fleet: groups of vehicles (operator x train type), ETCS on-board equipment and vehicle circulations.

* **Groups** come from the GTFS operator (agency) and train category: "Fernverkehr ICE",
  "Fernverkehr IC/EC", "RE/RB <operator>", "S-Bahn <operator>", "Güterverkehr". Small groups are pooled.
* **ETCS equipment** per train: trains that today run on an ETCS-only line are equipped (observed
  reality); all others are equipped with the group's assumed share (``EQUIPPED_SHARE``, assumptions,
  varied in the sensitivity analysis). Fitting a group (action ``fit:<group>``) equips all its trains.
* **Circulations** (which vehicle runs which trip next) are not published in GTFS. They are inferred:
  pieces of the same GTFS trip follow each other; at a terminus an arriving run is matched first-in-
  first-out to the next departure of the same group (same line first) after a minimum turnaround.
"""
from __future__ import annotations

import re
import zlib
from collections import defaultdict

# Assumed share of ETCS-equipped trains per group today (sources: DB fleet plans; ICE 3neo/ICE 4 and
# the ICE series used on the ETCS-only lines are fitted, most regional fleets are not).
EQUIPPED_SHARE = {"Fernverkehr ICE": 0.8, "Fernverkehr IC/EC": 0.3, "Güterverkehr": 0.3}
EQUIPPED_DEFAULT = {"FV": 0.3, "NV": 0.05, "S": 0.0, "GV": 0.3}

MIN_TURN = {"FV": 10.0, "NV": 5.0, "S": 3.0}      # min, minimum scheduled turnaround
MAX_TURN_WAIT = {"FV": 180.0, "NV": 120.0, "S": 90.0}
MIN_GROUP_TRAINS = 40

# vehicles: price of one train set and ETCS retrofit per set/loco (EUR m), assumptions with ranges
VEHICLE_PRICE_MEUR = {"FV-ICE": 35.0, "FV": 20.0, "NV": 10.0, "S": 8.0}
FIT_MEUR_PER_VEHICLE = {"FV": 0.6, "NV": 0.5, "S": 0.5, "GV": 0.4}
RESERVE_STANDBY_MEUR_YR = 0.5     # crew stand-by, stabling and upkeep of one hot reserve set
VEHICLE_LIFE_YEARS = 30
ETCS_ONBOARD_LIFE_YEARS = 25


def normalize_agency(a: str) -> str:
    a = str(a or "").strip()
    a = re.sub(r"^[0-9A-Z]{6}\s+", "", a)
    return a or "unbekannt"


def raw_group(cat: str, line: str, agency: str) -> str:
    if cat == "GV":
        return "Güterverkehr"
    if cat == "FV":
        p = "".join(ch for ch in str(line) if ch.isalpha()).upper()
        return "Fernverkehr ICE" if p in ("ICE", "ECE") else "Fernverkehr IC/EC"
    label = "RE/RB" if cat == "NV" else "S-Bahn"
    return f"{label} {normalize_agency(agency)}"


def assign_groups(trains, info: dict):
    """info: train id -> (line, agency). Sets tr.group, pooling small regional groups."""
    raw = {}
    for tr in trains:
        line, ag = info.get(tr.id, ("", ""))
        raw[tr.id] = raw_group(tr.ttype, line, ag)
    count = defaultdict(int)
    for g in raw.values():
        count[g] += 1
    for tr in trains:
        g = raw[tr.id]
        if count[g] < MIN_GROUP_TRAINS and tr.ttype in ("NV", "S"):
            g = "RE/RB übrige" if tr.ttype == "NV" else "S-Bahn übrige"
        tr.group = g


def base_share(group: str, cat: str) -> float:
    return EQUIPPED_SHARE.get(group, EQUIPPED_DEFAULT.get(cat, 0.0))


def assign_equipment(net, trains, share_override: dict | None = None):
    """Equip trains: base ETCS-only users always; fitted groups fully; otherwise group share."""
    share_override = share_override or {}
    for tr in trains:
        if tr.group in net.fitted:
            tr.equipped = True
            continue
        if any(net.segments[st[0]].etcs_only_base for st in tr.steps if st[0] in net.segments):
            tr.equipped = True
            continue
        share = share_override.get(tr.group, base_share(tr.group, tr.ttype))
        u = (zlib.crc32(f"etcs:{tr.id}".encode()) % 100000) / 100000
        tr.equipped = u < share


def build_circulations(trains):
    """Set tr.pred (index of the train whose vehicle runs this trip), tr.pred_kind ('cont' for the next
    piece of the same GTFS trip, 'turn' for a turnaround) and tr.turn (scheduled minutes between the
    predecessor's arrival and this departure). Also sets tr.hw_next: minutes to the next train of the
    same line in the same direction (used for the passenger penalty of a cancellation)."""
    idx = {tr.id: i for i, tr in enumerate(trains)}
    for tr in trains:
        tr.pred, tr.pred_kind, tr.turn = None, None, 0.0
    has_pred, has_succ = set(), set()
    # 1. pieces of the same GTFS trip
    pieces = defaultdict(list)
    for i, tr in enumerate(trains):
        m = re.match(r"^(fv|rv)-(.+)-(\d+)$", tr.id)
        if m:
            pieces[(m.group(1), m.group(2))].append((int(m.group(3)), i))
    for lst in pieces.values():
        lst.sort()
        for (_, a), (_, b) in zip(lst, lst[1:]):
            ta, tb = trains[a], trains[b]
            if tb.desired_dep >= ta.sched_arr:
                tb.pred, tb.pred_kind, tb.turn = a, "cont", tb.desired_dep - ta.sched_arr
                has_pred.add(b)
                has_succ.add(a)
    # 2. turnarounds at the terminus (same group; same line first, then any line of the group)
    for same_line in (True, False):
        arrivals = defaultdict(list)
        departures = defaultdict(list)
        for i, tr in enumerate(trains):
            if tr.ttype == "GV":
                continue
            k = (tr.stops[-1], tr.group, tr.line if same_line else "")
            if i not in has_succ:
                arrivals[k].append((tr.sched_arr, i))
            k2 = (tr.stops[0], tr.group, tr.line if same_line else "")
            if i not in has_pred:
                departures[k2].append((tr.desired_dep, i))
        for k, arr in arrivals.items():
            deps = sorted(departures.get(k, []))
            if not deps:
                continue
            used = [False] * len(deps)
            for ta, a in sorted(arr):
                cat = trains[a].ttype
                lo, hi = ta + MIN_TURN[cat], ta + MAX_TURN_WAIT[cat]
                for j, (td, b) in enumerate(deps):
                    if used[j] or td < lo or b in has_pred:
                        continue
                    if td > hi:
                        break
                    used[j] = True
                    tb = trains[b]
                    tb.pred, tb.pred_kind, tb.turn = a, "turn", td - ta
                    has_pred.add(b)
                    has_succ.add(a)
                    break
    # 3. headway to the next train of the same line and direction
    nxt = defaultdict(list)
    for i, tr in enumerate(trains):
        if tr.ttype != "GV":
            nxt[(tr.line, tr.stops[0], tr.stops[1])].append((tr.desired_dep, i))
    for lst in nxt.values():
        lst.sort()
        for (t, i), nx in zip(lst, lst[1:] + [None]):
            trains[i].hw_next = min(120.0, nx[0] - t) if nx else 120.0
    return idx


def fleet_sizes(trains) -> dict:
    """Vehicles per group: circulation chains (one vehicle each) plus 10% maintenance reserve;
    freight: 0.6 locomotives per daily train."""
    chains = defaultdict(int)
    gv = 0
    for tr in trains:
        if tr.ttype == "GV":
            gv += 1
        elif tr.pred is None:
            chains[tr.group] += 1
    out = {g: int(round(n * 1.1)) for g, n in chains.items()}
    out["Güterverkehr"] = int(round(gv * 0.6))
    return out


def group_category(group: str) -> str:
    if group.startswith("Fernverkehr"):
        return "FV"
    if group.startswith("S-Bahn"):
        return "S"
    if group == "Güterverkehr":
        return "GV"
    return "NV"
