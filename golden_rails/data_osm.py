"""Extract German railway lines from OpenStreetMap (Geofabrik state extracts).

Source: Geofabrik extracts mirrored on Hugging Face
(dataset NoeFlandre/osm-geofabrik-raw-pbf-extracts, snapshot 2026-08-31),
© OpenStreetMap contributors, ODbL 1.0.

For each federal state the PBF is downloaded, every way with railway=rail is kept
with its tags and geometry, written to data/raw/osm/rail_<state>.parquet, and the
PBF is deleted again. ``merge()`` combines all states into data/raw/osm/rail_de.parquet.

    python -m golden_rails.data_osm
"""
from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

import osmium
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "osm"
HF = "https://huggingface.co/datasets/NoeFlandre/osm-geofabrik-raw-pbf-extracts/resolve/main"
STATES = ["baden-wuerttemberg", "bayern", "berlin", "brandenburg", "bremen", "hamburg", "hessen",
          "mecklenburg-vorpommern", "niedersachsen", "nordrhein-westfalen", "rheinland-pfalz",
          "saarland", "sachsen", "sachsen-anhalt", "schleswig-holstein", "thueringen"]
TAGS = ["usage", "service", "tracks", "maxspeed", "electrified", "voltage", "highspeed", "railway:etcs",
        "railway:lzb", "railway:pzb", "name", "ref", "operator", "railway:track_ref", "passenger_lines",
        "railway:preferred_direction", "gauge"]


def _hav(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class RailHandler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.stations = []

    def way(self, w):
        if w.tags.get("railway") != "rail":
            return
        try:
            pts = [(n.ref, n.lat, n.lon) for n in w.nodes]
        except osmium.InvalidLocationError:
            return
        if len(pts) < 2:
            return
        length = sum(_hav(a[1], a[2], b[1], b[2]) for a, b in zip(pts[:-1], pts[1:]))
        row = {"way_id": w.id, "length_km": length,
               "node_ids": [p[0] for p in pts], "lats": [p[1] for p in pts], "lons": [p[2] for p in pts]}
        for k in TAGS:
            row[k] = w.tags.get(k)
        self.rows.append(row)

    def node(self, n):
        if n.tags.get("railway") in ("station", "halt") and n.tags.get("name"):
            self.stations.append({"node_id": n.id, "name": n.tags.get("name"), "railway": n.tags.get("railway"),
                                  "uic_ref": n.tags.get("uic_ref"), "railway_ref": n.tags.get("railway:ref"),
                                  "lat": n.location.lat, "lon": n.location.lon})


def extract_state(state: str, keep_pbf=False):
    out = RAW / f"rail_{state}.parquet"
    if out.exists():
        return out
    RAW.mkdir(parents=True, exist_ok=True)
    pbf = RAW / f"{state}-latest.osm.pbf"
    if not pbf.exists():
        subprocess.run(["curl", "-sSfL", "--retry", "4", "-o", str(pbf), f"{HF}/{state}-latest.osm.pbf"], check=True)
    h = RailHandler()
    h.apply_file(str(pbf), locations=True, idx="flex_mem")
    df = pd.DataFrame(h.rows)
    df["state"] = state
    df.to_parquet(out, index=False)
    st = pd.DataFrame(h.stations)
    st["state"] = state
    st.to_parquet(RAW / f"stations_{state}.parquet", index=False)
    if not keep_pbf:
        pbf.unlink()
    print(f"{state}: {len(df)} rail ways, {df.length_km.sum():.0f} km, {len(st)} stations", flush=True)
    return out


def merge():
    df = pd.concat([pd.read_parquet(RAW / f"rail_{s}.parquet") for s in STATES], ignore_index=True)
    df = df.drop_duplicates("way_id")      # ways crossing state borders appear twice
    df.to_parquet(RAW / "rail_de.parquet", index=False)
    st = pd.concat([pd.read_parquet(RAW / f"stations_{s}.parquet") for s in STATES], ignore_index=True)
    st.drop_duplicates("node_id").to_parquet(RAW / "stations_de.parquet", index=False)
    return df


def main():
    states = sys.argv[1:] or STATES
    for s in states:
        extract_state(s)
    if set(states) == set(STATES):
        df = merge()
        print(f"Germany: {len(df)} rail ways, {df.length_km.sum():.0f} km")


if __name__ == "__main__":
    main()
