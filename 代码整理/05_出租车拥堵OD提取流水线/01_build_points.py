r"""
把7天的逐点CSV(《是否堵车》的11个字段 + 一个新增的载客状态字段)转成点矢量文件(GeoPackage)，
X=longitude, Y=latitude 的普通二维点，所有字段(含congestion/occupied)都是普通属性字段。

不依赖 arcpy，纯开源栈：pandas + numpy + geopandas(pyogrio后端写GeoPackage)。

速度设计：
  1. pandas.read_csv(chunksize=...) 用C引擎分块解析，内存占用恒定
  2. 每块用 geopandas.points_from_xy 批量建点(向量化，不是逐行构造Point对象)
  3. 用 pyogrio 的 append 模式整块写入同一个 .gpkg，第一块新建，后续块追加
     (GDAL层面的批量写，不是逐行insert，速度跟arcpy版的NumPyArrayToFeatureClass同一个量级)

bad_coord==1 的行(占位0,0坐标)不生成点。

依赖安装:
    pip install pandas numpy geopandas pyogrio shapely

用法:
    python 01_build_points.py --base-dir <你的7天CSV所在目录>

列名默认值见 --help；如果你的CSV里 id/时间/经纬度/拥堵/载客 字段名不叫这几个默认名字，
用对应的 --xxx-col 参数覆盖，不用改代码。
"""
import argparse
import os
import time

import numpy as np
import pandas as pd
import geopandas as gpd

DEFAULT_BASE_DIR = r"E:\summercamp\出租车数据\是否堵车"
DEFAULT_DAYS = [f"201703{d:02d}" for d in range(1, 8)]
WGS84 = "EPSG:4326"

READ_DTYPES = {
    "group_id": str,
    "speed_gps_kmh": "float64",
    "trip_break": "int16",
    "out_of_day": "int16",
    "is_outlier_drift": "int16",
}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-dir", default=DEFAULT_BASE_DIR,
                    help="7天CSV所在的根目录，期望结构 <base-dir>\\<day>_data\\<day>_data.csv")
    p.add_argument("--days", nargs="+", default=DEFAULT_DAYS)
    p.add_argument("--chunk-size", type=int, default=2_000_000)
    p.add_argument("--id-col", default="taxi_id", help="车辆唯一id列名 (默认: taxi_id)")
    p.add_argument("--time-col", default="timestamp", help="时间戳列名，unix秒 (默认: timestamp)")
    p.add_argument("--lat-col", default="latitude", help="纬度列名 (默认: latitude)")
    p.add_argument("--lon-col", default="longitude", help="经度列名 (默认: longitude)")
    p.add_argument("--congestion-col", default="congestion", help="拥堵标记列名，0/1 (默认: congestion)")
    p.add_argument("--pickup-col", default="occupied", help="载客状态列名，0/1 (默认: occupied)")
    p.add_argument("--bad-coord-col", default="bad_coord",
                    help="无效坐标标记列名，=1的行不生成点；传空字符串 '' 表示不做这个过滤 (默认: bad_coord)")
    return p.parse_args()


def build_read_dtypes(args):
    dtypes = dict(READ_DTYPES)
    dtypes[args.id_col] = str
    dtypes[args.time_col] = "int32"
    dtypes[args.lat_col] = "float64"
    dtypes[args.lon_col] = "float64"
    dtypes[args.congestion_col] = "int16"
    dtypes[args.pickup_col] = "int16"
    if args.bad_coord_col:
        dtypes[args.bad_coord_col] = "int16"
    return dtypes


def build_day(base_dir, day, args, read_dtypes):
    day_dir = os.path.join(base_dir, f"{day}_data")
    csv_path = os.path.join(day_dir, f"{day}_data.csv")
    out_path = os.path.join(day_dir, "points.gpkg")
    layer_name = "pts"
    done_marker = os.path.join(day_dir, ".points_done")

    if not os.path.exists(csv_path):
        print(f"[{day}] 找不到 {csv_path}，跳过")
        return None

    if os.path.exists(done_marker) and os.path.exists(out_path):
        print(f"[{day}] 已有完成标记，跳过转点(如需重跑请先删除 {done_marker} 和 {out_path})")
        return out_path

    if os.path.exists(out_path):
        print(f"[{day}] {out_path} 已存在但没有完成标记，视为上次中断的残留，删除重建")
        os.remove(out_path)

    t0 = time.time()
    total_rows = 0
    written = 0
    first_chunk = True

    reader = pd.read_csv(csv_path, dtype=read_dtypes, chunksize=args.chunk_size)
    for i, df in enumerate(reader):
        total_rows += len(df)
        if args.bad_coord_col:
            df = df[df[args.bad_coord_col] == 0]
        n_valid = len(df)
        if n_valid == 0:
            continue

        geometry = gpd.points_from_xy(df[args.lon_col], df[args.lat_col])
        gdf = gpd.GeoDataFrame(df, geometry=geometry, crs=WGS84)

        gdf.to_file(out_path, layer=layer_name, driver="GPKG", append=not first_chunk)
        first_chunk = False

        written += n_valid
        print(f"[{day}] 第{i+1}块: 累计读取={total_rows:,} 累计写入={written:,} 耗时={time.time()-t0:.1f}s")

    print(f"[{day}] 转点完成. 源文件行数={total_rows:,} 写入={written:,} 耗时={time.time()-t0:.1f}s")

    if written > 0:
        with open(done_marker, "w", encoding="utf-8") as f:
            f.write(f"rows={written}\n")
        return out_path
    return None


def main():
    args = parse_args()
    read_dtypes = build_read_dtypes(args)
    for day in args.days:
        build_day(args.base_dir, day, args, read_dtypes)


if __name__ == "__main__":
    main()
