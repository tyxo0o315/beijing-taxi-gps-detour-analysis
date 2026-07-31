# -*- coding: utf-8 -*-
"""
把一份"一行一趟行程、起终点各占一组列(start_lon/start_lat/start_time 和
end_lon/end_lat/end_time)"的宽表(CSV或点/表格shp都行)，拆成"起点+终点都在同一份
点矢量文件里、用 point_type 字段区分"的长表，方便在GIS里同时看到起点和终点，并用
trip_id 把同一趟行程的起讫点关联起来。

除了 start_*/end_* 这两组字段，其余字段(比如你在ArcGIS里做完空间连接/统计后新增的
字段)会原样各复制一份到起点行和终点行，不会丢。

用法:
    python build_od_points.py --input <day>_trips.csv --output <day>_od_points.shp
    python build_od_points.py --input trip0301.shp --output trip0301_od_points.shp
"""
import argparse
import os

import pandas as pd
import geopandas as gpd

WGS84 = "EPSG:4326"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True, help="输入文件，.csv 或 geopandas能读的矢量格式(.shp/.gpkg等)")
    p.add_argument("--output", required=True)
    return p.parse_args()


def load_table(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return pd.read_csv(path), None
    gdf = gpd.read_file(path)
    crs = gdf.crs
    df = pd.DataFrame(gdf.drop(columns="geometry"))
    return df, crs


def main():
    args = parse_args()
    df, crs = load_table(args.input)
    if crs is None:
        crs = WGS84

    start_cols = [c for c in df.columns if c.startswith("start_")]
    end_cols = [c for c in df.columns if c.startswith("end_")]
    common_cols = [c for c in df.columns if c not in start_cols and c not in end_cols]

    starts = df[common_cols].copy()
    starts["point_type"] = "start"
    for c in start_cols:
        starts[c[len("start_"):]] = df[c]

    ends = df[common_cols].copy()
    ends["point_type"] = "end"
    for c in end_cols:
        ends[c[len("end_"):]] = df[c]

    long_df = pd.concat([starts, ends], ignore_index=True)
    if "trip_id" in long_df.columns:
        long_df = long_df.sort_values(["trip_id", "point_type"])

    missing = [c for c in ("lon", "lat") if c not in long_df.columns]
    if missing:
        raise SystemExit(f"输入表里缺少 start_{'/start_'.join(missing)} 或 end_{'/end_'.join(missing)} 这组列，"
                          f"无法确定经纬度，找到的列有: {df.columns.tolist()}")

    gdf_out = gpd.GeoDataFrame(long_df, geometry=gpd.points_from_xy(long_df["lon"], long_df["lat"]), crs=crs)
    gdf_out.to_file(args.output)
    n_trips = len(df)
    print(f"{args.output}: {n_trips:,}行(趟) -> {len(gdf_out):,}个点(起点{n_trips:,} + 终点{n_trips:,})")


if __name__ == "__main__":
    main()
