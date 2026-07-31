r"""
(d) 低速/疑似拥堵点密度。开源版，逻辑对应arcpy版06脚本(同样没有acc_on字段可用，
见01_feature_engineering.py顶部注释)。

用法:
    python 06_density_low_speed.py
    python 06_density_low_speed.py --speed-threshold 15 --min-dt-sec 10
"""
import argparse
import os
import sys
import time
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import FEATURES_CSV, ensure_out_dir
from kde_utils import HistogramAccumulator, lonlat_to_utm
from boundary_utils import beijing_mask

CHUNK_SIZE = 2_000_000
DTYPES = {
    "latitude": "float64", "longitude": "float64",
    "trip_break": "int8", "dt_sec": "float64", "speed_gps_kmh": "float64",
}
USECOLS = list(DTYPES)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=FEATURES_CSV)
    p.add_argument("--speed-threshold", type=float, default=10.0)
    p.add_argument("--min-dt-sec", type=int, default=8)
    p.add_argument("--cell-size", type=float, default=None)
    p.add_argument("--radius", type=float, default=None)
    return p.parse_args()


def main():
    args = parse_args()
    out_dir = ensure_out_dir()
    acc = HistogramAccumulator(cell_size=args.cell_size, search_radius=args.radius)

    t0 = time.time()
    total = 0
    for chunk in pd.read_csv(args.input, usecols=USECOLS, dtype=DTYPES, chunksize=CHUNK_SIZE):
        sub = chunk[
            beijing_mask(chunk["longitude"].values, chunk["latitude"].values)
            & (chunk["trip_break"] == 0)
            & (chunk["dt_sec"] >= args.min_dt_sec)
            & (chunk["speed_gps_kmh"] >= 0) & (chunk["speed_gps_kmh"] < args.speed_threshold)
        ]
        if len(sub):
            x, y = lonlat_to_utm(sub["longitude"].values, sub["latitude"].values)
            acc.add(x, y)
        total += len(chunk)
        print(f"已处理 {total:,} 行, 耗时 {time.time()-t0:.1f}s")

    out_path = os.path.join(out_dir, "kd_low_speed.tif")
    acc.finalize(out_path)
    print(f"低速点密度(<{args.speed_threshold}km/h, dt>={args.min_dt_sec}s):", out_path)


if __name__ == "__main__":
    main()
