<p align="right"><a href="README.zh-CN.md">中文</a></p>

<h1 align="center">Beijing Taxi GPS Detour Analysis</h1>

<p align="center">
<img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License">
<img src="https://img.shields.io/badge/python-3.9%2B-blue.svg" alt="Python">
<img src="https://img.shields.io/badge/domain-GIS%20%2F%20Spatial%20Data%20Science-green.svg" alt="GIS">
</p>

An 8-stage geospatial data pipeline that turns raw Beijing taxi GPS traces into a **congestion-induced detour signal**: from millions of raw GPS pings, it extracts persistent traffic hotspots, detects congestion segments, reconstructs origin-destination (OD) trips, matches them to the road network, computes theoretically optimal routes, and finally flags trips whose real trajectory was significantly longer than the shortest possible route.

This was a **team project**; this repository collects the pipeline stages and reference material it was built with.

## Pipeline overview

```mermaid
flowchart LR
    A["01 Data Preprocessing<br/>raw TSV → sorted core CSV"] --> B["02 Density & Hotspot Analysis<br/>KDE surfaces, hex grid,<br/>persistent-hotspot mining"]
    B --> C["04 Congestion Detection<br/>drop long parking segments,<br/>flag low-speed runs"]
    C --> D["05 Taxi Congestion OD Extraction<br/>pickup/dropoff state machine"]
    D --> E["06 Road-Grade Matching<br/>snap points to nearest road,<br/>label AM/PM peak"]
    E --> F["07 Optimal Routing<br/>shortest path + alt-route set"]
    F --> G["08 Trajectory ∩ Optimal-Path<br/>detour ratio labeling"]
    B -. optional, requires<br/>ArcGIS Pro + arcpy .-> H["03 ArcGIS-native<br/>Space-Time Cube"]

    classDef stage fill:#eef4ff,stroke:#4a6cf7,color:#1a1a2e;
    classDef optional fill:#fff3e0,stroke:#f5a623,color:#1a1a2e,stroke-dasharray: 4 3;
    class A,B,C,D,E,F,G stage;
    class H optional;
```

Two root-level notebooks, [`taxi_gps_pipeline_zh.ipynb`](taxi_gps_pipeline_zh.ipynb) (Chinese) and [`taxi_gps_pipeline_en.ipynb`](taxi_gps_pipeline_en.ipynb) (English), are a single-file, bilingual mirror of the exact same 8-stage pipeline (66 cells each). They default to a safe dry-run mode (`EXECUTE_PIPELINE = False`) and gracefully skip missing optional imports, so they're a good place to start reading before diving into the individual scripts.

## Repository structure

```
.
├── taxi_gps_pipeline_zh.ipynb         # Master pipeline notebook (Chinese)
├── taxi_gps_pipeline_en.ipynb         # Master pipeline notebook (English)
└── pipeline/
    ├── 01_data_preprocessing/                       # Stage 1 — raw TSV → sorted CSV
    ├── 02_density_hotspot_analysis_opensource/       # Stage 2 — KDE density, hex grid, persistent hotspots, open-source (no arcpy)
    ├── 03_arcgis_space_time_cube/                    # Optional — arcpy/ArcGIS Pro space-time cube
    ├── 04_congestion_detection/                      # Stage 3 — congestion segment detection
    ├── 05_taxi_congestion_od_extraction/             # Stage 4 — pickup/dropoff (OD) trip extraction
    ├── 06_road_grade_matching/                       # Stage 5 — snap GPS points to nearest road + time-period label
    ├── 07_optimal_routing/                           # Stage 6 — shortest path + near-optimal route set
    └── 08_trajectory_optimal_path_intersection/      # Stage 7 — real trajectory vs. optimal path → detour label
```

Each subfolder has its own `README.md` with the exact CLI usage, input/output schema, and method notes for that stage.

## Requirements

Almost the entire pipeline is pure open-source Python (`geopandas`, `shapely`, `rasterio`, `networkx`, `pandas`, `numpy`, `pyproj`, `pyogrio`, `tqdm`) and can be reproduced on any standard machine or cloud/CI runner. Two exceptions require a licensed **ArcGIS Pro** install with `arcpy`:

- `pipeline/03_arcgis_space_time_cube/` — the entire folder (native `arcpy.stpm` space-time cube)
- `pipeline/05_taxi_congestion_od_extraction/build_congestion_points.py` — an arcpy variant of point-to-feature conversion; the fully equivalent open-source version `01_build_points.py` in the same folder does **not** require arcpy

Install the core open-source dependencies with:

```bash
pip install numpy pandas pyproj rasterio geopandas shapely networkx pyogrio tqdm
```

(`pipeline/02_density_hotspot_analysis_opensource/requirements.txt` lists the minimal set needed for that stage specifically.)

## Data

This repository does **not** contain any real/raw taxi trip GPS records — those are private and excluded on purpose. It only ships the public reference geodata the pipeline needs: Beijing province/county administrative boundaries (`02_density_hotspot_analysis_opensource/boundary/`) and a 2017 Beijing road network shapefile (`06_road_grade_matching/2017_Beijing_road.*`), plus a synthetic-data demo notebook (`05_taxi_congestion_od_extraction/notebook/taxi_od_pipeline_part1-5.ipynb`) you can run end-to-end without any real data.

## License

Released under the [MIT License](LICENSE).
