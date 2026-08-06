# Data Notes

This repository is designed to show the pipeline structure without publishing private taxi GPS records.

## Included Reference Data

- `pipeline/02_density_hotspot_analysis_opensource/boundary/`: Beijing administrative boundary shapefiles used for masking and district assignment.
- `pipeline/05_taxi_congestion_od_extraction/study_area/`: study-area polygon used by the OD extraction stage.
- `pipeline/06_road_grade_matching/2017_Beijing_road.*`: road-network shapefile used for road-grade matching and routing experiments.

## Excluded Data

Raw taxi trajectory records and intermediate outputs derived from them are not committed. To reproduce the full pipeline, provide your own taxi GPS table with the columns documented in each stage README, then write outputs to local working directories that are ignored by Git.

## Reproducibility Path

1. Install dependencies from `requirements.txt` or `environment.yml`.
2. Open `taxi_gps_pipeline_en.ipynb` or `taxi_gps_pipeline_zh.ipynb`.
3. Keep `EXECUTE_PIPELINE = False` for a guided dry run, or switch it on after configuring local input paths.
4. Use `pipeline/05_taxi_congestion_od_extraction/notebook/taxi_od_pipeline_part1-5.ipynb` for the synthetic OD extraction demo.

## License Boundary

Code is released under MIT. Third-party geospatial datasets remain under their original data-provider terms; verify those terms before reusing them outside this repository.
