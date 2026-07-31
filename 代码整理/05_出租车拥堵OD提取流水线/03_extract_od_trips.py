"""
Extract per-trip origin/destination points from point-level taxi GPS data.

A "trip" is one contiguous run of pickup-status == occupied for a given vehicle
id, whose FIRST row is preceded by an empty ("no passenger") row that was
congested. The trip's start point is that first occupied row; its end point is
the first empty row immediately after the run. Trips are found independently
per vehicle, so a vehicle with multiple pickup runs yields multiple trips.

Usage:
    python extract_od_trips.py --input points.csv --output trips.csv

All column names are configurable (see --help) so this works against any CSV
with the right *shape*, not a fixed header.
"""
import argparse
import math

import numpy as np
import pandas as pd

EARTH_RADIUS_KM = 6371.0088
# Sanity bounds for is_plausible: implied straight-line average speed above
# this, OR a trip lasting longer than this, is treated as physically
# unrealistic for a single taxi trip. The duration cap exists because a
# too-long duration alone drives the implied speed *down*, so the speed
# check alone doesn't catch it (seen in real data: a source timestamp
# corrupted by exactly +6 years produced a "plausible" ~200 m/h trip that
# actually lasted 189 million seconds).
MAX_PLAUSIBLE_KMH = 150.0
MAX_PLAUSIBLE_DURATION_SEC = 86400  # 1 day


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", required=True, help="Path to input CSV (one GPS point per row)")
    p.add_argument("--output", default="trips.csv", help="Path to write completed trips (default: trips.csv)")
    p.add_argument("--incomplete-output", default="incomplete_trips.csv",
                    help="Path to write trips with a start but no observed end (default: incomplete_trips.csv)")
    p.add_argument("--id-col", default="id", help="Vehicle id column name (default: id)")
    p.add_argument("--time-col", default="timestamp", help="Timestamp column name (default: timestamp)")
    p.add_argument("--speed-col", default="avg_speed", help="Average speed column name (default: avg_speed, unused by the algorithm itself, kept for reference/output)")
    p.add_argument("--lon-col", default="lon", help="Longitude column name (default: lon)")
    p.add_argument("--lat-col", default="lat", help="Latitude column name (default: lat)")
    p.add_argument("--congestion-col", default="congestion", help="Congestion flag column name (default: congestion)")
    p.add_argument("--pickup-col", default="pickup", help="Pickup/occupied status column name (default: pickup)")
    p.add_argument("--occupied-value", default="1",
                    help="Value in --pickup-col meaning 'has passenger'; every other value is treated as "
                         "'empty' (default: 1). Compared as string, so works whether the column is numeric or text.")
    p.add_argument("--already-sorted", action="store_true",
                    help="Skip sorting by (id, time); pass this only if the input is already sorted that way.")
    p.add_argument("--trip-points-output", default=None,
                    help="Optional: also write every raw GPS point belonging to a completed trip "
                         "(start..end inclusive, i.e. the intermediate trajectory points too), tagged "
                         "with trip_id and a within-trip sequence number. Format is picked from the "
                         "extension: .gpkg / .shp / .geojson. Requires geopandas.")
    p.add_argument("--trip-lines-output", default=None,
                    help="Optional: also write one LineString per completed trip, connecting all its "
                         "points (start->intermediate->end) in time order, ready to use as a trajectory "
                         "line. Format from extension: .gpkg / .shp / .geojson. Requires geopandas.")
    return p.parse_args()


def haversine_km(lon1, lat1, lon2, lat2):
    lon1, lat1, lon2, lat2 = map(np.radians, (lon1, lat1, lon2, lat2))
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2.0) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def find_trips_for_vehicle(occ, congestion):
    """occ: bool array (True = occupied/has passenger), congestion: array aligned with occ.

    Returns (trips, incomplete) where trips is a list of (start_idx, end_idx)
    row-index pairs into the vehicle's own (already time-sorted) slice, and
    incomplete is a list of start_idx with no matching end.
    """
    n = len(occ)
    trips = []
    incomplete = []
    if n < 2:
        return trips, incomplete

    change_idx = np.where(occ[1:] != occ[:-1])[0] + 1
    run_starts = np.concatenate(([0], change_idx))
    run_ends = np.concatenate((change_idx, [n]))  # exclusive

    for rs, re in zip(run_starts, run_ends):
        if not occ[rs]:
            continue  # this run is an "empty" run, not a pickup run
        if rs == 0:
            # No preceding row in this vehicle's data to check the congestion
            # precondition against -> can't confirm a congested origin, skip.
            continue
        if congestion[rs - 1] != 1:
            continue  # origin wasn't congested -> not a qualifying trip
        start_idx = rs
        if re < n:
            trips.append((start_idx, re))  # re is the first empty row after the run
        else:
            incomplete.append(start_idx)  # run runs off the end of the data
    return trips, incomplete


def main():
    args = parse_args()

    df = pd.read_csv(args.input)

    required = [args.id_col, args.time_col, args.lon_col, args.lat_col, args.congestion_col, args.pickup_col]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise SystemExit(f"Input is missing required column(s): {missing}. Found columns: {list(df.columns)}")

    if not args.already_sorted:
        df = df.sort_values([args.id_col, args.time_col], kind="mergesort")

    df = df.reset_index(drop=True)
    occupied_value = args.occupied_value
    occ_all = df[args.pickup_col].astype(str) == str(occupied_value)
    congestion_all = pd.to_numeric(df[args.congestion_col], errors="coerce")

    want_trajectory = bool(args.trip_points_output or args.trip_lines_output)

    trip_rows = []
    incomplete_rows = []
    trajectory_rows = []  # only populated if want_trajectory
    trip_id = 0

    for vid, idx in df.groupby(args.id_col, sort=False).indices.items():
        idx = np.sort(np.asarray(idx))
        occ = occ_all.to_numpy()[idx]
        congestion = congestion_all.to_numpy()[idx]

        trips, incomplete = find_trips_for_vehicle(occ, congestion)

        sub = df.iloc[idx]
        for start_i, end_i in trips:
            trip_id += 1
            srow = sub.iloc[start_i]
            erow = sub.iloc[end_i]
            duration_sec = float(erow[args.time_col]) - float(srow[args.time_col])
            dist_km = haversine_km(srow[args.lon_col], srow[args.lat_col], erow[args.lon_col], erow[args.lat_col])
            implied_kmh = (dist_km / (duration_sec / 3600.0)) if duration_sec > 0 else float("inf")
            is_plausible = bool(
                0 < duration_sec <= MAX_PLAUSIBLE_DURATION_SEC and implied_kmh <= MAX_PLAUSIBLE_KMH
            )
            trip_rows.append({
                "trip_id": trip_id,
                args.id_col: vid,
                "start_time": srow[args.time_col],
                "start_lon": srow[args.lon_col],
                "start_lat": srow[args.lat_col],
                "end_time": erow[args.time_col],
                "end_lon": erow[args.lon_col],
                "end_lat": erow[args.lat_col],
                "duration_sec": duration_sec,
                "straight_line_km": dist_km,
                "is_plausible": is_plausible,
            })

            if want_trajectory:
                # start_i..end_i inclusive: the full point-by-point trajectory for
                # this trip, including every intermediate GPS ping, not just the
                # two endpoints.
                span = sub.iloc[start_i:end_i + 1]
                for seq, (_, prow) in enumerate(span.iterrows()):
                    trajectory_rows.append({
                        "trip_id": trip_id,
                        args.id_col: vid,
                        "seq": seq,
                        "is_endpoint": seq == 0 or seq == len(span) - 1,
                        "time": prow[args.time_col],
                        "lon": prow[args.lon_col],
                        "lat": prow[args.lat_col],
                    })

        for start_i in incomplete:
            srow = sub.iloc[start_i]
            incomplete_rows.append({
                args.id_col: vid,
                "start_time": srow[args.time_col],
                "start_lon": srow[args.lon_col],
                "start_lat": srow[args.lat_col],
            })

    trips_df = pd.DataFrame(trip_rows)
    incomplete_df = pd.DataFrame(incomplete_rows)

    trips_df.to_csv(args.output, index=False)
    incomplete_df.to_csv(args.incomplete_output, index=False)

    n_implausible = int((~trips_df["is_plausible"]).sum()) if len(trips_df) else 0
    print(f"Vehicles processed: {df[args.id_col].nunique()}")
    print(f"Completed trips: {len(trips_df)} (of which {n_implausible} flagged is_plausible=False)")
    print(f"Incomplete trips (start with no observed end): {len(incomplete_df)}")
    print(f"Wrote: {args.output}, {args.incomplete_output}")

    if want_trajectory:
        traj_df = pd.DataFrame(trajectory_rows)
        write_trajectory_outputs(traj_df, trips_df, args)


def _geo_driver_for(path):
    ext = path.rsplit(".", 1)[-1].lower()
    return {"gpkg": "GPKG", "shp": "ESRI Shapefile", "geojson": "GeoJSON", "json": "GeoJSON"}.get(ext)


def write_trajectory_outputs(traj_df, trips_df, args):
    try:
        import geopandas as gpd
    except ImportError as exc:
        raise SystemExit(
            "--trip-points-output/--trip-lines-output need geopandas (pip install geopandas pyogrio shapely)"
        ) from exc

    if args.trip_points_output:
        driver = _geo_driver_for(args.trip_points_output)
        if driver is None:
            raise SystemExit(f"Unrecognized extension for --trip-points-output: {args.trip_points_output}")
        gdf = gpd.GeoDataFrame(
            traj_df, geometry=gpd.points_from_xy(traj_df["lon"], traj_df["lat"]), crs="EPSG:4326"
        )
        gdf.to_file(args.trip_points_output, driver=driver)
        print(f"Wrote {args.trip_points_output}: {len(gdf):,} trajectory points across {trips_df['trip_id'].nunique():,} trips")

    if args.trip_lines_output:
        driver = _geo_driver_for(args.trip_lines_output)
        if driver is None:
            raise SystemExit(f"Unrecognized extension for --trip-lines-output: {args.trip_lines_output}")
        from shapely.geometry import LineString

        lines = []
        for tid, group in traj_df.sort_values(["trip_id", "seq"]).groupby("trip_id", sort=False):
            coords = list(zip(group["lon"], group["lat"]))
            if len(coords) < 2:
                continue  # degenerate (shouldn't happen: a trip always has >=2 points)
            lines.append({"trip_id": tid, "n_points": len(coords), "geometry": LineString(coords)})
        lines_df = pd.DataFrame(lines).merge(
            trips_df[["trip_id", args.id_col, "start_time", "end_time", "duration_sec",
                      "straight_line_km", "is_plausible"]],
            on="trip_id", how="left",
        )
        gdf = gpd.GeoDataFrame(lines_df, geometry="geometry", crs="EPSG:4326")
        gdf.to_file(args.trip_lines_output, driver=driver)
        print(f"Wrote {args.trip_lines_output}: {len(gdf):,} trip trajectory lines")


if __name__ == "__main__":
    main()
