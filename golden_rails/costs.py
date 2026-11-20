"""Capex model for widening a corridor (adding tracks) and for ETCS.

Cost of one added track-km =
    construction:  BASE x terrain factor x land-use complexity (share-weighted)
  + land:          added corridor width x 1 km x land price (share-weighted)
  + ETCS:          constant per track-km (independent of terrain and land use)

Corridor width: 2 x 6 m shoulders/embankment + 4.5 m per track, so every added
track widens the corridor by TRACK_SPACING_M.
"""
from __future__ import annotations

from .network import Segment

BASE_MEUR_PER_TRACK_KM = 12.0     # earthworks, track, catenary, drainage, structures on flat open land
TRACK_SPACING_M = 4.5
SHOULDER_M = 6.0

TERRAIN_FACTOR = {"flat": 1.0, "rolling": 1.3, "hilly": 1.9, "mountainous": 3.2}

# land-use classes: construction complexity multiplier and land price (EUR/m2)
LANDUSE = {
    "urban":     {"complexity": 2.8, "land_eur_m2": 700.0},  # dense built-up: demolition, noise walls, night work
    "suburban":  {"complexity": 1.6, "land_eur_m2": 180.0},  # built-up, lower density
    "open":      {"complexity": 1.0, "land_eur_m2": 6.0},    # unbuilt agricultural land
    "protected": {"complexity": 1.3, "land_eur_m2": 6.0},    # forest / nature reserve: compensation measures
}

ETCS_MEUR_PER_TRACK_KM = 1.0      # constant: ETCS L2 + share of digital interlocking


def corridor_width_m(tracks: int) -> float:
    return 2 * SHOULDER_M + tracks * TRACK_SPACING_M


def landuse_shares(seg: Segment) -> dict:
    return {"urban": seg.urban_share, "suburban": seg.suburban_share,
            "open": seg.open_share, "protected": seg.protected_share}


def track_addition_cost(seg: Segment, n_added: int, etcs_on_new: bool = True) -> dict:
    """EUR million for adding ``n_added`` tracks over the whole segment."""
    sh = landuse_shares(seg)
    complexity = sum(sh[k] * LANDUSE[k]["complexity"] for k in sh)
    land_eur_m2 = sum(sh[k] * LANDUSE[k]["land_eur_m2"] for k in sh)
    tkm = seg.length * n_added
    construction = tkm * BASE_MEUR_PER_TRACK_KM * TERRAIN_FACTOR[seg.terrain] * complexity
    added_width = corridor_width_m(seg.tracks + n_added) - corridor_width_m(seg.tracks)
    land = added_width * seg.length * 1000 * land_eur_m2 / 1e6
    etcs = tkm * ETCS_MEUR_PER_TRACK_KM if etcs_on_new else 0.0
    return {"construction": construction, "land": land, "etcs": etcs,
            "total": construction + land + etcs, "track_km": tkm}


def etcs_upgrade_cost(seg: Segment) -> dict:
    """EUR million to equip all existing tracks of a segment with ETCS (constant per track-km)."""
    tkm = seg.length * seg.tracks
    c = tkm * ETCS_MEUR_PER_TRACK_KM
    return {"construction": 0.0, "land": 0.0, "etcs": c, "total": c, "track_km": 0.0}
