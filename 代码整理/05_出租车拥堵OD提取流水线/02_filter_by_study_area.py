r"""
按研究区面筛选车辆：同一辆车(taxi_id)当天只要有任意一个"载客中(occupied=1)"的点落在
研究区面内，就保留该车当天的全部GPS点；否则整车当天的数据都丢弃。

为什么保留"当天全部点"而不是只保留那一段轨迹：下一步(03_extract_od_trips.py)的起终点
状态机需要看到完整的前后文(拥堵空客行->接客行、接客行->空客行)才能正确判断起终点，只截
一段轨迹会把这些上下文切掉。

不依赖 arcpy，纯开源栈：pandas + numpy + shapely(向量化 contains_xy 做点在面内判断，
不是逐点构造Point对象再判断，速度快)。

做法(对每一天分两趟扫描原始CSV，内存占用恒定)：
  第1趟：分块读CSV，用 shapely.contains_xy 判断每个点是否落在研究区面内，
         再叠加 occupied==1 的条件，把满足的行的 taxi_id 去重后收集成"合格车辆"集合
  第2趟：再分块读一次CSV，保留 taxi_id 落在"合格车辆"集合里的所有行(不管occupied是几)，
         写成 <day>_filtered.csv

依赖安装:
    pip install pandas numpy shapely geopandas pyogrio   (geopandas/pyogrio只用来读研究区shp)

用法:
    python 02_filter_by_study_area.py --base-dir <7天CSV所在目录> --study-area-shp <研究区面路径>
"""
import argparse
import os
import time

import numpy as np
import pandas as pd
import geopandas as gpd
import shapely

DEFAULT_BASE_DIR = r"E:\summercamp\出租车数据\是否堵车"
DEFAULT_DAYS = [f"201703{d:02d}" for d in range(1, 8)]
WGS84 = "EPSG:4326"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-dir", default=DEFAULT_BASE_DIR,
                    help="7天CSV所在的根目录，期望结构 <base-dir>\\<day>_data\\<day>_data.csv")
    p.add_argument("--days", nargs="+", default=DEFAULT_DAYS)
    p.add_argument("--study-area-shp", required=True, help="研究区面文件路径(.shp/.gpkg等geopandas能读的都行)")
    p.add_argument("--chunk-size", type=int, default=2_000_000)
    p.add_argument("--id-col", default="taxi_id")
    p.add_argument("--lat-col", default="latitude")
    p.add_argument("--lon-col", default="longitude")
    p.add_argument("--pickup-col", default="occupied")
    p.add_argument("--occupied-value", type=int, default=1)
    p.add_argument("--bad-coord-col", default="bad_coord",
                    help="无效坐标标记列名，=1的行不会写进筛选后的输出(占位0,0坐标会污染03步的"
                         "起终点坐标)；传空字符串 '' 表示不做这个过滤 (默认: bad_coord)")
    return p.parse_args()


def load_study_area_geometry(path):
    gdf = gpd.read_file(path)
    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(WGS84)
    geom = gdf.geometry.union_all() if hasattr(gdf.geometry, "union_all") else gdf.geometry.unary_union
    shapely.prepare(geom)
    return geom


def find_qualified_ids(csv_path, geom, args):
    qualified = set()
    total_rows = 0
    reader = pd.read_csv(
        csv_path, chunksize=args.chunk_size,
        usecols=[args.id_col, args.lat_col, args.lon_col, args.pickup_col],
        dtype={args.id_col: str, args.lat_col: "float64", args.lon_col: "float64", args.pickup_col: "int16"},
    )
    for df in reader:
        total_rows += len(df)
        inside = shapely.contains_xy(geom, df[args.lon_col].to_numpy(), df[args.lat_col].to_numpy())
        hit = inside & (df[args.pickup_col].to_numpy() == args.occupied_value)
        if hit.any():
            qualified.update(df.loc[hit, args.id_col].unique().tolist())
    return qualified, total_rows


def write_filtered_csv(csv_path, out_path, qualified_ids, args):
    written = 0
    first = True
    reader = pd.read_csv(csv_path, chunksize=args.chunk_size, dtype={args.id_col: str})
    for df in reader:
        keep = df[df[args.id_col].isin(qualified_ids)]
        if args.bad_coord_col:
            keep = keep[keep[args.bad_coord_col] == 0]
        if len(keep) == 0:
            continue
        keep.to_csv(out_path, mode="w" if first else "a", header=first, index=False)
        first = False
        written += len(keep)
    if first:
        # 一行都没匹配到，仍然写一个只有表头的空文件，方便下游脚本/人工确认不是漏跑
        pd.read_csv(csv_path, nrows=0).to_csv(out_path, index=False)
    return written


def process_day(base_dir, day, geom, args):
    day_dir = os.path.join(base_dir, f"{day}_data")
    csv_path = os.path.join(day_dir, f"{day}_data.csv")
    qualified_ids_path = os.path.join(day_dir, "qualified_ids.csv")
    filtered_path = os.path.join(day_dir, f"{day}_filtered.csv")
    done_marker = os.path.join(day_dir, ".filtered_done")

    if not os.path.exists(csv_path):
        print(f"[{day}] 找不到 {csv_path}，跳过")
        return None

    if os.path.exists(done_marker) and os.path.exists(filtered_path):
        print(f"[{day}] 已有完成标记，跳过筛选(如需重跑请先删除 {done_marker})")
        return filtered_path

    t0 = time.time()
    qualified, total_rows = find_qualified_ids(csv_path, geom, args)
    print(f"[{day}] 第1趟完成: 源文件{total_rows:,}行, 落在研究区内且occupied="
          f"{args.occupied_value}的合格车辆数={len(qualified):,}, 耗时={time.time()-t0:.1f}s")

    pd.DataFrame({args.id_col: sorted(qualified)}).to_csv(qualified_ids_path, index=False)

    written = write_filtered_csv(csv_path, filtered_path, qualified, args)
    print(f"[{day}] 第2趟完成: 保留{written:,}行(合格车辆当天全部数据) -> {filtered_path} "
          f"总耗时={time.time()-t0:.1f}s")

    with open(done_marker, "w", encoding="utf-8") as f:
        f.write(f"qualified_vehicles={len(qualified)}\nrows={written}\n")
    return filtered_path


def main():
    args = parse_args()
    geom = load_study_area_geometry(args.study_area_shp)
    for day in args.days:
        process_day(args.base_dir, day, geom, args)


if __name__ == "__main__":
    main()
