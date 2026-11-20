# golden-rails

**Question:** which kilometre of new railway line in Germany buys the most delay reduction per euro?
**KPI:** total train-delay minutes saved per year ÷ annual cost (capex annuity + opex).

## Answer (current model run)

**A third track between Lüneburg and Uelzen (Hamburg–Hannover main line, 35 km)** ranks first.

| | |
|---|---|
| Delay saved | ≈ 2,600 min/day → **0.86 million min/year** (±6%, 95% CI) |
| Annual cost | €25.1 m (capex €770 m × annuity 2.67% + 35 track-km × €130k opex) |
| **KPI** | **34 delay-min saved per €1,000** (95% CI 30–38) ≈ **€29 per delay minute avoided** |
| Per km | ≈ 24,600 delay-min/year per km of new track |

It stays #1 with incident rates halved and doubled (`results/sensitivity_incidents.csv`). Its lead over #2 is about 36%,
so the unit cost would need to be about 36% higher than assumed before it lost first place.

Why this km: Lüneburg–Uelzen carries ~230 trains/day on two tracks: 80 long-distance, 38 regional, 110 freight
(Hamburg port hinterland). It is the first two-track section after the three-track Maschen–Lüneburg stretch.
Fast ICEs get stuck behind 100 km/h freight trains, so every small perturbation spreads. A third, bidirectional
track lets trains overtake and keeps one track open in each direction during a track fault. This agrees with
the planning debate: this section is the core of "Alpha-E".

### Top of the ranking (60 Monte-Carlo days each, common random numbers)

| # | Candidate | km | Capex €m | min saved/yr | €/min saved |
|---|---|---|---|---|---|
| 1 | +1 track Lüneburg–Uelzen | 35 | 770 | 0.86 m | 29 |
| 2 | +1 track Mannheim–Karlsruhe | 70 | 1,540 | 1.27 m | 40 (wide CI) |
| 3 | **Alpha-E: 3rd track Lüneburg–Uelzen–Celle** (planned project) | 85 | 2,000 | 1.51 m | 43 |
| 4 | +1 track Uelzen–Celle | 50 | 1,100 | 0.82 m | 44 |
| 5 | +1 track Nürnberg–Würzburg | 100 | 2,500 | 1.78 m | 45 |
| 6 | +1 track Rotenburg–Bremen | 45 | 990 | 0.71 m | 45 |
| 7 | +1 track Hanau–Gelnhausen | 20 | 600 | 0.38 m | 49 |

### Planned projects (BVWP / Deutschlandtakt), same KPI

| Project | km | Capex €m | min saved/yr | €/min saved |
|---|---|---|---|---|
| Alpha-E 3rd track Lüneburg–Uelzen–Celle | 85 | 2,000 | 1.51 m | 43 |
| NBS Nürnberg–Würzburg | 80 | 5,000 | 2.12 m | 73 |
| NBS Mannheim–Karlsruhe | 70 | 4,000 | 1.50 m | 83 |
| NBS Hamburg–Hannover (A7) | 125 | 9,000 | 3.04 m | 90 |
| NBS Frankfurt–Mannheim | 85 | 5,500 | 1.71 m | 99 |
| ABS/NBS Hanau–Gelnhausen–Fulda | 65 | 4,300 | 1.09 m | 121 |
| Rheintalbahn Offenburg–Freiburg 4-track | 65 | 4,500 | 0.91 m | 150 |
| NBS Fulda–Gerstungen | 60 | 4,000 | 0.69 m | 177 |
| NBS Ulm–Augsburg | 80 | 5,500 | 0.45 m | 372 |
| RRX Duisburg–Dortmund 4-track | 52 | 3,000 | 0.24 m | 398 |
| Emmerich–Oberhausen 3rd track | 70 | 2,500 | 0.13 m | 587 |
| Nürnberg–Bamberg 4-track | 60 | 2,000 | 0.06 m | 1,143 |
| NBS Hannover–Bielefeld | 95 | 9,000 | 0.20 m | 1,360 |
| 2nd track Rotenburg–Verden | 25 | 500 | 0.01 m | 1,390 |
| 2nd track Münster–Lünen | 60 | 900 | 0.02 m | 1,491 |

Hannover–Bielefeld scores low because the existing line is already four-track and not delay-critical in the model.
Its case rests on travel time (Deutschlandtakt), which this KPI deliberately ignores.

Full results: `results/candidates.csv`. Figures: `results/kpi_ranking.png`, `results/network_map.png`.

## Method

1. **Network** (`data/segments.csv`, 106 segments, 76 nodes): the German core network, with track count,
   line speed, permitted train types (FV long-distance / NV regional / GV freight) and regional trains per hour.
   Segments are split into blocks: 10 km on single track (passing-loop spacing), 20 km otherwise.
2. **Current timetable** (`data/fv_lines.csv`, `data/freight_flows.csv`): 27 ICE/IC/EC lines,
   20 freight corridors and regional services. That is about 7,900 trains/day.
   A planner books conflict-free paths by priority (FV, then NV, then GV) with a 3-min headway, a 1-min buffer and a 5% running-time supplement.
   On single track, opposing trains must be separated.
3. **Standard disruptions**, each sampled per simulated day:
   * origin delays: FV 30% of trains, mean 4.5 min; NV 25%, mean 5.5 min; GV 50%, mean 25 min (all exponential);
   * dwell extensions at stops (20% of stops, mean 1 min) and ±3% running-time noise;
   * track faults: 1.0 per 1,000 track-km per day, one track blocked, median 45 min;
     on double track this forces wrong-line working on the other track;
   * full closures: 0.15 per 1,000 route-km per day, all tracks blocked, median 75 min (persons on the track, police, trees).
     FV and GV trains reroute when a detour is faster.

   The primary-delay parameters are **calibrated** to DB's published punctuality. Baseline result:
   FV 61.6% (DB 2024 ≈ 62%) and NV 90.1% (≈ 90%) of trains less than 6 min late.
4. **Operations**: event-driven, first-come-first-served dispatching on the same block model.
   Trains never run early. Delay counts at every scheduled stop for FV, and at the destination for NV and GV.
5. **Scenarios** keep the **current timetable fixed**. Only trains whose route changes (e.g. ICEs moving onto a
   new high-speed line) get new paths, slotted into the gaps. So the KPI measures robustness, not timetable re-design.
   Each scenario runs the same 60 random days as the baseline (common random numbers). The difference is paired, and its standard error is reported.
6. **Cost**: capex annuity at 1.7% over 60 years (BVWP 2030 convention) plus €130k per new track-km per year.
   Planned-project capex values are rounded public estimates (`data/candidates.csv`).
   Generic "+1 track" costs use the per-km unit costs in `segments.csv`: €18–60 m/km depending on terrain and urban setting.

## Caveats — read before quoting numbers

* **Curated data, not GTFS/OSM.** This sandbox could not reach download.gtfs.de or OSM, so network, line
  frequencies and freight volumes are hand-curated approximations of the 2025/26 network. The structure is built to
  ingest the real GTFS timetable and OSM track data; doing so is the most valuable next step.
* **Stations are not capacity-limited.** Real delay hotspots also sit in nodes (Köln, Hamburg, Frankfurt, Mannheim).
  Node projects are out of scope here and may beat any line km.
* No passenger weighting, connections or rolling-stock and crew turnarounds; construction sites are not modelled.
  Demand is held fixed, so added capacity is used only for robustness, not for more trains.
* Cost estimates for NBS projects are uncertain by ±50%. The KPI scales with 1/cost.

## Run

```bash
pip install -r requirements.txt
python -m golden_rails.evaluate --seeds 60          # ~10 min on 4 cores
python -m golden_rails.evaluate --seeds 30 --incident-scale 2 --tag _inc2
python -m golden_rails.plots
pytest -q
```
