r"""
把《是否堵车》目录下 1-7 天的逐点CSV(group_id/taxi_id/timestamp/latitude/longitude/
speed_gps_kmh/trip_break/bad_coord/out_of_day/is_outlier_drift/congestion)转成点要素类，
X=longitude, Y=latitude 的普通二维点，congestion 跟其它字段一样是普通属性字段(不进geometry)，
分别写入各自天数文件夹下的 congestion_points.gdb。

速度设计
--------
不用逐行 arcpy.da.InsertCursor(Python for-loop 调 C API，31M行 x 7天在这台机器上会是明显
瓶颈)，改用分块批量写入：
  1. pandas.read_csv(chunksize=...) 用C引擎解析(比手写 line.split(",") 快得多)
  2. 每块转成命名 numpy 结构化数组，用 arcpy.da.NumPyArrayToFeatureClass 整块写入
     (真正的批量写，比逐行 InsertCursor 快一个数量级)
  3. 第一块直接建目标要素类；后续块写到 memory 工作空间的临时要素类，再用
     arcpy.management.Append(..., schema_type="NO_TEST") 追加进目标要素类，然后删除临时表
这样内存占用只跟 chunksize 有关(常数)，不会像"一把梭"XYTableToPoint 那样在16GB机器上
被系统 SIGKILL(参考 02_build_points.py 里记录的教训)。

bad_coord==1 的行(占位的 0,0 坐标)不生成点，只统计跳过数量；总行数/跳过数/写入数都在
单次分块扫描里累计，不用像 02_build_points.py 那样额外整个文件预扫一遍来算期望行数。
每天完成后写一个 .done 标记文件，重跑脚本时已完成的天会直接跳过。

用法:
    "D:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" build_congestion_points.py
    (可选 --base-dir / --days / --chunk-size 用于指向测试数据或调小分块，见 --help)
"""
import argparse
import os
import time

import numpy as np
import pandas as pd
import arcpy

DEFAULT_BASE_DIR = r"E:\summercamp\出租车数据\是否堵车"
DEFAULT_DAYS = [f"201703{d:02d}" for d in range(1, 8)]
WGS84_WKID = 4326

DTYPE = np.dtype([
    ("group_id", "<U16"), ("taxi_id", "<U32"), ("timestamp", "<i4"),
    ("latitude", "<f8"), ("longitude", "<f8"), ("speed_gps_kmh", "<f8"),
    ("trip_break", "<i2"), ("bad_coord", "<i2"), ("out_of_day", "<i2"),
    ("is_outlier_drift", "<i2"), ("congestion", "<i2"),
])
READ_DTYPES = {
    "group_id": str, "taxi_id": str, "timestamp": "int32",
    "latitude": "float64", "longitude": "float64", "speed_gps_kmh": "float64",
    "trip_break": "int16", "bad_coord": "int16", "out_of_day": "int16",
    "is_outlier_drift": "int16", "congestion": "int16",
}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-dir", default=DEFAULT_BASE_DIR)
    p.add_argument("--days", nargs="+", default=DEFAULT_DAYS)
    p.add_argument("--chunk-size", type=int, default=2_000_000)
    return p.parse_args()


def setup_env(base_dir):
    scratch_dir = os.path.join(base_dir, "scratch")
    os.makedirs(scratch_dir, exist_ok=True)
    arcpy.env.scratchWorkspace = scratch_dir
    arcpy.env.overwriteOutput = True


def chunk_to_array(df):
    arr = np.empty(len(df), dtype=DTYPE)
    for name in DTYPE.names:
        arr[name] = df[name].to_numpy()
    return arr


def build_day(base_dir, day, chunk_size):
    day_dir = os.path.join(base_dir, f"{day}_data")
    csv_path = os.path.join(day_dir, f"{day}_data.csv")
    gdb_path = os.path.join(day_dir, "congestion_points.gdb")
    fc_path = os.path.join(gdb_path, "congestion_pts")
    done_marker = os.path.join(day_dir, ".congestion_pts_done")

    if not os.path.exists(csv_path):
        print(f"[{day}] 找不到 {csv_path}，跳过")
        return

    if os.path.exists(done_marker) and arcpy.Exists(fc_path):
        print(f"[{day}] 已有完成标记 {done_marker}，跳过(如需重跑请先删除该文件)")
        return

    if not arcpy.Exists(gdb_path):
        arcpy.management.CreateFileGDB(day_dir, "congestion_points.gdb")
    if arcpy.Exists(fc_path):
        print(f"[{day}] {fc_path} 已存在但没有完成标记，视为上次中断的残留，删除重建")
        arcpy.management.Delete(fc_path)

    sr = arcpy.SpatialReference(WGS84_WKID)
    scratch_fc = "memory\\chunk_tmp"

    t0 = time.time()
    total_rows = 0
    written = 0
    first_chunk = True

    reader = pd.read_csv(csv_path, dtype=READ_DTYPES, chunksize=chunk_size)
    for i, df in enumerate(reader):
        total_rows += len(df)
        df = df[df["bad_coord"] == 0]
        n_valid = len(df)

        if n_valid == 0:
            continue
        arr = chunk_to_array(df)

        if first_chunk:
            arcpy.da.NumPyArrayToFeatureClass(arr, fc_path, ["longitude", "latitude"], sr)
            first_chunk = False
        else:
            if arcpy.Exists(scratch_fc):
                arcpy.management.Delete(scratch_fc)
            arcpy.da.NumPyArrayToFeatureClass(arr, scratch_fc, ["longitude", "latitude"], sr)
            arcpy.management.Append(scratch_fc, fc_path, schema_type="NO_TEST")

        written += n_valid
        print(f"[{day}] 第{i+1}块: 累计读取={total_rows:,} 累计写入={written:,} "
              f"耗时={time.time()-t0:.1f}s")

    if arcpy.Exists(scratch_fc):
        arcpy.management.Delete(scratch_fc)

    skipped_bad_coord = total_rows - written
    final_count = int(arcpy.management.GetCount(fc_path)[0]) if arcpy.Exists(fc_path) else 0
    ok = final_count == written
    print(f"[{day}] 完成. 源文件行数={total_rows:,} 跳过bad_coord={skipped_bad_coord:,} "
          f"写入={final_count:,}(应为{written:,}) 耗时={time.time()-t0:.1f}s "
          f"[{'OK' if ok else '行数不一致,需要人工检查'}]")

    if ok:
        with open(done_marker, "w", encoding="utf-8") as f:
            f.write(f"rows={final_count}\n")


def main():
    args = parse_args()
    setup_env(args.base_dir)
    for day in args.days:
        build_day(args.base_dir, day, args.chunk_size)
    print("全部完成.")


if __name__ == "__main__":
    main()
