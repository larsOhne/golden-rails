<p align="center"><img src="assets/logo.svg" alt="golden rails logo: a golden award statuette holding a rail, standing on a train wheel" width="220"></p>

# golden-rails

**Question:** where in the German rail network does new infrastructure buy the most delay reduction per euro?

**KPI:** cumulative **passenger delay** saved per year ÷ annual cost (capex annuity + opex).
Passenger delay = Σ over trains and stops of *arrival delay × passengers expected to alight there*.
Demand is held fixed, so ranking by this is identical to ranking by average delay per expected passenger.

## Answer (current model run)

### Most valuable new track km: **a 3rd track Mannheim–Karlsruhe (70 km)**

| | |
|---|---|
| Capex (cost model) | €1.35 bn: construction €1.23 bn, land €52 m, ETCS on the new track €70 m |
| Passenger delay saved | **75.6 m passenger-min/year**; average delay per passenger −11 s network-wide |
| KPI | **1,670 passenger-min per €1,000** (95% CI 1,140–2,210) ≈ **€0.60 per passenger-minute** |
| Who gains | ICE (109k pax-min/day), RE (73k), S-Bahn Rhein-Neckar (47k) |

Mannheim–Karlsruhe carries ~280 trains/day on two tracks: ICE, RE, S-Bahn and ~160 freight trains (Rhine–Alpine corridor).
Under the priority rule the S-Bahn and freight wait for every ICE. A third track lets them be overtaken without stopping.

The next three are close, with overlapping confidence intervals:

| # | Option | km | Capex €m | pax-min saved/yr | pax-min/€1,000 |
|---|---|---|---|---|---|
| 1 | +1 track Mannheim–Karlsruhe | 70 | 1,351 | 75.6 m | 1,673 (1,139–2,208) |
| 2 | +1 track Hanau–Gelnhausen | 20 | 411 | 17.3 m | 1,274 (949–1,598) |
| 3 | +1 track Lüneburg–Uelzen | 35 | 634 | 24.8 m | 1,154 (979–1,329) |
| 4 | +1 track Uelzen–Celle | 50 | 856 | 29.0 m | 989 (891–1,087) |
| – | best planned project: Alpha-E 3rd track Lüneburg–Uelzen–Celle | 85 | 2,000 | 48.6 m | 753 (690–816) |

A second extra track (+2) on the same corridors roughly halves the KPI: the first added track delivers most of the benefit.

### Cheaper than any new km: ETCS

ETCS on an existing corridor (headway 3 → 2 min, constant €1 m per track-km) is the strongest lever. On the same
corridor it beats a third track by **5–6×**, and the best ETCS options beat the best track options by about 6×.
Examples: ETCS Hanau–Gelnhausen at ≈ 9,900 pax-min/€1,000, Mannheim–Karlsruhe ≈ 9,800, Lüneburg–Uelzen ≈ 7,100.
With a more conservative 2.5-min ETCS headway the advantage shrinks to 3–5× but holds
(`results/candidates_etcs2.5.csv`).

ETCS and a new track are **substitutes, not complements**. Adding the track *after* ETCS is in place is still worth it on
Mannheim–Karlsruhe (945 pax-min/€1,000), Lüneburg–Uelzen (599) and Hanau–Gelnhausen (567), see
`results/track_after_etcs_tracks.csv`. Recommended order: ETCS first, then the third track on these corridors.

### Planned projects (BVWP / Deutschlandtakt), published capex

| Project | km | Capex €m | pax-min saved/yr | pax-min/€1,000 |
|---|---|---|---|---|
| Alpha-E 3rd track Lüneburg–Uelzen–Celle | 85 | 2,000 | 48.6 m | 753 |
| NBS Mannheim–Karlsruhe | 70 | 4,000 | 75.0 m | 599 |
| ABS/NBS Hanau–Gelnhausen–Fulda | 65 | 4,300 | 49.9 m | 379 |
| NBS Hamburg–Hannover (A7) | 125 | 9,000 | 102.3 m | 375 |
| NBS Nürnberg–Würzburg | 80 | 5,000 | 57.5 m | 372 |
| NBS Frankfurt–Mannheim | 85 | 5,500 | 47.1 m | 278 |
| RRX 4-track Duisburg–Dortmund | 52 | 3,000 | 20.6 m | 220 |
| Nürnberg–Bamberg 4-track | 60 | 2,000 | 14.3 m | 207 |
| 2nd track Rotenburg–Verden | 25 | 500 | 2.8 m | 166 |
| Rheintalbahn Offenburg–Freiburg 4-track | 65 | 4,500 | 20.3 m | 148 |
| NBS Fulda–Gerstungen | 60 | 4,000 | 17.4 m | 142 |
| 2nd track Münster–Lünen | 60 | 900 | 2.5 m | 79 |
| NBS Ulm–Augsburg | 80 | 5,500 | 10.1 m | 60 |
| NBS Hannover–Bielefeld | 95 | 9,000 | 7.8 m | 29 |
| Emmerich–Oberhausen 3rd track | 70 | 2,500 | 1.4 m | 19 |

Figures: `results/kpi_ranking_new_track.png`, `results/kpi_ranking_all.png`, `results/network_map.png`, `results/punctuality_cost_curve.png`.


## Was kostet 95 % Pünktlichkeit? (`golden_rails/punctuality.py`)

Die Korridormaßnahmen werden in der Reihenfolge ihres Kosten-Nutzen-Werts zu immer größeren Paketen kumuliert.
Jedes Paket wird simuliert (30 Tage). Pünktlichkeit = Anteil < 6 min verspätet, wie bei DB.

| Infrastrukturpaket | Capex | Jahreskosten | ICE/IC (Halte) | Alle Fahrgäste |
|---|---|---|---|---|
| Heute | – | – | 61 % | 86,7 % |
| bis €2 Mrd. | €1,9 Mrd. | €52 Mio. | 67 % | 88,5 % |
| bis €8 Mrd. | €8,0 Mrd. | €236 Mio. | 71 % | 90,4 % |
| bis €32 Mrd. | €31,5 Mrd. | €1,0 Mrd. | 77 % | 92,4 % |
| bis €128 Mrd. | €128 Mrd. | €4,1 Mrd. | 85 % | 94,4 % |
| bis €256 Mrd. | €254 Mrd. | €8,1 Mrd. | 88 % | **95,1 %** |
| Maximum: ETCS + 2 Gleise überall | €354 Mrd. | €11,2 Mrd. | **88 % (Obergrenze)** | 95,2 % |

* **95 % aller Fahrgäste nur mit Infrastruktur:** ≈ €250 Mrd., also praktisch jeder Korridor ausgebaut. Unrealistisch.
* **95 % im Fernverkehr nur mit Infrastruktur:** nicht erreichbar, maximal ≈ 88 %. Den Rest verursachen
  Primärverspätungen (verspätete Abfahrt, verlängerte Haltezeiten), die kein Gleis verhindert.
* **Kombination** (`results/punctuality_levers.csv`): Anteil der Züge mit Primärverspätung reduziert um …

| | −50 % | −75 % | −90 % |
|---|---|---|---|
| heutige Infrastruktur: ICE/IC / alle Fahrgäste | 76 % / 92,1 % | 85 % / **95,3 %** | 92 % / 97,4 % |
| + Paket €8 Mrd. | 84 % / 94,7 % | 91 % / 97,0 % | **95,0 %** / 98,4 % |
| + Paket €32 Mrd. | 88 % / **95,9 %** | 93 % / 97,7 % | **96,4 %** / 98,8 % |

  Weniger Störungen an der Infrastruktur selbst (Weichen, Signale, Personen im Gleis) bringen nur 1–2 Punkte.

**Lesart:** 95 % der Fahrgäste ≈ **€8–32 Mrd. Infrastruktur plus Halbierung der Primärverspätungen**.
95 % im Fernverkehr ≈ **€8–32 Mrd. plus 75–90 % weniger Primärverspätungen**.

**Wichtige Einschränkung:** Das Modell kennt keine Knoten (Köln, Frankfurt, Hamburg) und keine Baustellen. Bei der
Kalibrierung landet deshalb ein Teil der realen infrastrukturbedingten Verspätung in den „Primärverspätungen“.
Der Infrastrukturanteil ist damit eher unterschätzt. Die Kosten der betrieblichen Hebel (Fahrzeugreserve,
Wartung, Personal, Haltezeitmanagement) sind nicht bepreist.

## Corridor model

* **Corridor width = number of tracks.** Each corridor (106 segments, `data/segments.csv`) has n tracks:
  n//2 per direction, and an odd track is bidirectional (overtaking / counter-peak).
  Any width is allowed, so options of +1 and +2 tracks are tested everywhere, including 4 → 6.
* **Train types and the standard priority rule:** ICE/IC > RE/RB > S-Bahn/tram-train > cargo.
  The rule applies when the timetable is built, and in operations: a dispatcher looks 15 min ahead and holds a
  lower-priority train if it would delay an approaching higher-priority train on the same track.
  Priority dispatching lifts ICE punctuality by ~12 points at the expense of freight (tested in `tests/`).
* **Signalling:** headway 3 min conventional, 2 min with ETCS L2 (`GR_HEADWAY_ETCS` to override).
  New infrastructure is always built with ETCS.
* **Timetable:** 27 ICE/IC lines with expected loads, 20 freight corridors, RE and S-Bahn/tram-train services:
  ~8,000 trains/day. The current timetable is kept fixed in every scenario; only trains whose route changes
  (e.g. onto a new line) are re-slotted.
* **Standard disruptions:** origin delays, dwell extensions, running-time noise, track faults (one track blocked,
  forcing wrong-line working) and full closures with rerouting.
  Calibrated to DB punctuality: ICE/IC 61.8% (DB ≈ 62%), RE 90.4% (≈ 90%), S-Bahn 92%, cargo 74%.
* **Passengers:** load per train by line (160–420 on ICE/IC; RE 140, S-Bahn 90, more in metro areas),
  times a peak/off-peak profile. Long-distance passengers alight evenly over a line's stops.
  Freight has zero passenger weight by default (`FREIGHT_PAX_EQUIV`); its train-delay minutes are still reported.

## Cost model (`golden_rails/costs.py`)

Cost of one added track-km:

```
construction = €12 m × terrain factor × land-use complexity
land         = added corridor width (4.5 m per track) × 1 km × land price
ETCS         = €1 m per track-km, constant (independent of terrain and land use)
```

| Terrain | factor | | Land use | complexity | land €/m² |
|---|---|---|---|---|---|
| flat | 1.0 | | open (unbuilt) | 1.0 | 6 |
| rolling | 1.3 | | protected / forest | 1.3 | 6 |
| hilly | 1.9 | | suburban (built) | 1.6 | 180 |
| mountainous | 3.2 | | urban (dense built) | 2.8 | 700 |

Each corridor has a terrain class and land-use shares in `segments.csv`. Resulting costs range from about
€14 m/km (flat, open) through about €31 m/km (Ruhr, urban) to about €54 m/km (Middle Rhine gorge).

Annualisation: 1.7% discount rate over 60 years (BVWP 2030), plus €130k opex per added track-km and year.

## Caveats

* **Curated data, not GTFS/OSM.** The sandbox had no access to the national timetable feed or OSM. Track counts,
  frequencies, freight volumes, passenger loads, terrain and land-use shares are hand-curated approximations.
* Stations and nodes have no capacity limit; node projects (Köln, Hamburg, Frankfurt) are out of scope.
* No transfers, rolling-stock or crew turnarounds, or construction sites. Demand is fixed.
* ETCS: no migration or vehicle-fitment costs, and only the headway effect is modelled.
* Planned-project capex values are rounded public estimates (±50%).

## Run

```bash
pip install -r requirements.txt
python -m golden_rails.evaluate                     # screen 439 scenarios (16 days), refine top 30 (60 days), ~25 min on 4 cores
python -m golden_rails.evaluate --refine-options T1,T2,T1E --top 20 --tag _tracks   # refine track-building options
GR_HEADWAY_ETCS=2.5 python -m golden_rails.evaluate --refine-options E,T1 --top 24 --seeds 40 --tag _etcs2.5
python -m golden_rails.punctuality             # cost to reach 95 % punctuality, ~8 min
python -m golden_rails.plots
pytest -q
```
