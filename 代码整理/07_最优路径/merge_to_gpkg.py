"""Join per-OD path Shapefiles with vehicle/time fields and export one GPKG."""

from __future__ import annotations

from pathlib import Path
import re

import geopandas as gpd
import pandas as pd
from tqdm.auto import tqdm


PROJECT_ROOT = Path(__file__).resolve().parent
TRIP_DIR = PROJECT_ROOT / "trip"
RESULT_ROOT = PROJECT_ROOT / "downloaded_random_two_paths_20260724"
SHARD_DIRS = [
    RESULT_ROOT / "output" / "random_two_paths_shard0",
    RESULT_ROOT / "output" / "random_two_paths_shard1",
]
OUTPUT_GPKG = RESULT_ROOT / "optimal_path_sets.gpkg"
OUTPUT_LAYER = "optimal_paths"

FINAL_COLUMNS = [
    "route_id",
    "taxi_id",
    "trip_date",
    "trip_id",
    "start_time",
    "end_time",
    "alt_rank",
    "dist_m",
    "ratio",
    "is_best",
    "geometry",
]


def trip_date_from_filename(path: Path) -> str:
    match = re.search(r"03(0[1-6])", path.stem)
    if not match:
        raise ValueError(f"无法从文件名识别日期: {path.name}")
    return f"2017-03-{match.group(1)}"


def load_trip_lookup() -> pd.DataFrame:
    files = sorted(TRIP_DIR.glob("trip*.shp"))
    if not files:
        raise FileNotFoundError(f"没有找到原始trip图层: {TRIP_DIR}")

    frames = []
    for path in files:
        frame = gpd.read_file(
            path,
            columns=["trip_id", "taxi_id", "start_time", "end_time"],
        )
        trip_date = trip_date_from_filename(path)
        frame["trip_id"] = pd.to_numeric(
            frame["trip_id"], errors="raise"
        ).astype("int64")
        frame["start_time"] = pd.to_numeric(
            frame["start_time"], errors="raise"
        ).astype("int64")
        frame["end_time"] = pd.to_numeric(
            frame["end_time"], errors="raise"
        ).astype("int64")
        frame["taxi_id"] = frame["taxi_id"].astype(str)
        frame["trip_date"] = trip_date
        frame["route_id"] = (
            trip_date.replace("-", "") + "_" + frame["trip_id"].astype(str)
        )
        frames.append(
            frame[
                [
                    "route_id",
                    "taxi_id",
                    "trip_date",
                    "trip_id",
                    "start_time",
                    "end_time",
                ]
            ]
        )

    lookup = pd.concat(frames, ignore_index=True)
    if lookup["route_id"].duplicated().any():
        raise ValueError("原始trip图层中存在重复route_id")
    return lookup


def load_paths() -> gpd.GeoDataFrame:
    files = []
    for shard_dir in SHARD_DIRS:
        if not shard_dir.exists():
            raise FileNotFoundError(f"结果分片目录不存在: {shard_dir}")
        files.extend(sorted(shard_dir.rglob("paths.shp")))
    if not files:
        raise FileNotFoundError("两个结果分片中均未找到paths.shp")

    frames = []
    for path in tqdm(files, desc="读取路径Shapefile"):
        frames.append(
            gpd.read_file(
                path,
                columns=[
                    "route_id",
                    "alt_rank",
                    "dist_m",
                    "ratio",
                    "is_best",
                ],
            )
        )

    paths = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True),
        geometry="geometry",
        crs=frames[0].crs,
    )
    paths["route_id"] = paths["route_id"].astype(str)
    paths["alt_rank"] = pd.to_numeric(
        paths["alt_rank"], errors="raise"
    ).astype("int64")
    paths["dist_m"] = pd.to_numeric(
        paths["dist_m"], errors="raise"
    ).astype("float64")
    paths["ratio"] = pd.to_numeric(
        paths["ratio"], errors="raise"
    ).astype("float64")
    paths["is_best"] = paths["is_best"].astype(bool)
    return paths


def main() -> None:
    lookup = load_trip_lookup()
    paths = load_paths()

    counts = paths.groupby("route_id").size()
    if counts.max() > 2:
        raise ValueError(f"单个route_id最多发现{int(counts.max())}条路径")

    result = paths.merge(
        lookup,
        on="route_id",
        how="left",
        validate="many_to_one",
    )
    link_columns = [
        "taxi_id",
        "trip_date",
        "trip_id",
        "start_time",
        "end_time",
    ]
    missing = result[link_columns].isna().any(axis=1)
    if missing.any():
        examples = result.loc[missing, "route_id"].head(10).tolist()
        raise ValueError(
            f"{int(missing.sum())}条路径未关联到车辆或时间，例如: {examples}"
        )

    result = gpd.GeoDataFrame(
        result[FINAL_COLUMNS],
        geometry="geometry",
        crs=paths.crs,
    ).sort_values(
        ["trip_date", "trip_id", "alt_rank"],
        kind="stable",
        ignore_index=True,
    )
    if result.geometry.isna().any() or result.geometry.is_empty.any():
        raise ValueError("合并结果中存在空路径几何")

    result.to_file(
        OUTPUT_GPKG,
        layer=OUTPUT_LAYER,
        driver="GPKG",
        encoding="UTF-8",
    )

    check = gpd.read_file(OUTPUT_GPKG, layer=OUTPUT_LAYER)
    if len(check) != len(result):
        raise AssertionError("GeoPackage写入前后的记录数不一致")
    if list(check.columns) != FINAL_COLUMNS:
        raise AssertionError(f"GeoPackage字段不符合要求: {list(check.columns)}")
    if check["route_id"].nunique() != paths["route_id"].nunique():
        raise AssertionError("GeoPackage写入前后的route_id数量不一致")

    print(f"输出文件: {OUTPUT_GPKG}")
    print(f"图层名称: {OUTPUT_LAYER}")
    print(f"路径记录: {len(check):,}")
    print(f"route_id: {check['route_id'].nunique():,}")
    print(f"坐标系: {check.crs}")
    print(f"字段: {list(check.columns)}")


if __name__ == "__main__":
    main()
