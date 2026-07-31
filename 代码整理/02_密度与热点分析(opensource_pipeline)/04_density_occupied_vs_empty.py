r"""
(b) 空车 vs 重车对比密度。开源版，逻辑对应arcpy版04脚本。

occupied=0(所谓"空车密度")实际是"非重车"的合集(空车+驻车+停运+任务车+未知)，这是
上游convert_to_csv.py收窄字段带来的信息损失，不是本脚本引入的，详见
01_feature_engineering.py顶部注释。

用法:
    python 04_density_occupied_vs_empty.py
"""
import argparse
import os
import sys
import time
import numpy as np
import pandas as pd
import rasterio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import FEATURES_CSV, ensure_out_dir
from kde_utils import HistogramAccumulator, lonlat_to_utm
from boundary_utils import beijing_mask

CHUNK_SIZE = 2_000_000
DTYPES = {"latitude": "float64", "longitude": "float64", "occupied": "int8"}
USECOLS = ["latitude", "longitude", "occupied"]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=FEATURES_CSV)
    p.add_argument("--cell-size", type=float, default=None)
    p.add_argument("--radius", type=float, default=None)
    p.add_argument("--no-diff", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    out_dir = ensure_out_dir()
    acc_occ = HistogramAccumulator(cell_size=args.cell_size, search_radius=args.radius)
    acc_empty = HistogramAccumulator(cell_size=args.cell_size, search_radius=args.radius)

    t0 = time.time()
    total = 0
    for chunk in pd.read_csv(args.input, usecols=USECOLS, dtype=DTYPES, chunksize=CHUNK_SIZE):
        chunk = chunk[beijing_mask(chunk["longitude"].values, chunk["latitude"].values)]
        occ = chunk[chunk["occupied"] == 1]
        empty = chunk[chunk["occupied"] == 0]
        if len(occ):
            x, y = lonlat_to_utm(occ["longitude"].values, occ["latitude"].values)
            acc_occ.add(x, y)
        if len(empty):
            x, y = lonlat_to_utm(empty["longitude"].values, empty["latitude"].values)
            acc_empty.add(x, y)
        total += len(chunk)
        print(f"已处理 {total:,} 行, 耗时 {time.time()-t0:.1f}s")

    occ_path = os.path.join(out_dir, "kd_occupied.tif")
    empty_path = os.path.join(out_dir, "kd_empty.tif")
    occ_density, _ = acc_occ.finalize(occ_path)
    empty_density, _ = acc_empty.finalize(empty_path)
    print("重车(occupied=1)密度:", occ_path)
    print("非重车(occupied=0，含空车/驻车/停运/任务车/未知)密度:", empty_path)

    if not args.no_diff:
        diff = occ_density - empty_density
        diff_path = os.path.join(out_dir, "kd_occupied_minus_empty.tif")
        with rasterio.open(occ_path) as ref:
            profile = ref.profile
        profile.update(dtype="float32")
        with rasterio.open(diff_path, "w", **profile) as dst:
            dst.write(diff.astype(np.float32), 1)
        print("差值栅格(重车-非重车，正值=真实需求主导):", diff_path)


if __name__ == "__main__":
    main()
