r"""
(f) 上下客(OD)事件密度。开源版，逻辑对应arcpy版08脚本。pickup/dropoff判定比旧
arcpy pipeline宽松(任意非重车->重车都算pickup)，见01_feature_engineering.py顶部注释。

用法:
    python 08_density_od_events.py
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
DTYPES = {"latitude": "float64", "longitude": "float64", "event_type": "object"}
USECOLS = list(DTYPES)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=FEATURES_CSV)
    p.add_argument("--cell-size", type=float, default=None)
    p.add_argument("--radius", type=float, default=None)
    return p.parse_args()


def main():
    args = parse_args()
    out_dir = ensure_out_dir()
    acc_pickup = HistogramAccumulator(cell_size=args.cell_size, search_radius=args.radius)
    acc_dropoff = HistogramAccumulator(cell_size=args.cell_size, search_radius=args.radius)

    t0 = time.time()
    total = 0
    for chunk in pd.read_csv(args.input, usecols=USECOLS, dtype=DTYPES, chunksize=CHUNK_SIZE,
                              keep_default_na=False):
        chunk = chunk[beijing_mask(chunk["longitude"].values, chunk["latitude"].values)]
        pickup = chunk[chunk["event_type"] == "pickup"]
        dropoff = chunk[chunk["event_type"] == "dropoff"]
        if len(pickup):
            x, y = lonlat_to_utm(pickup["longitude"].values, pickup["latitude"].values)
            acc_pickup.add(x, y)
        if len(dropoff):
            x, y = lonlat_to_utm(dropoff["longitude"].values, dropoff["latitude"].values)
            acc_dropoff.add(x, y)
        total += len(chunk)
        print(f"已处理 {total:,} 行, 耗时 {time.time()-t0:.1f}s")

    pickup_path = os.path.join(out_dir, "kd_pickup.tif")
    dropoff_path = os.path.join(out_dir, "kd_dropoff.tif")
    acc_pickup.finalize(pickup_path)
    acc_dropoff.finalize(dropoff_path)
    print(f"上车点密度(累计{acc_pickup.n_points_added:,}个):", pickup_path)
    print(f"下车点密度(累计{acc_dropoff.n_points_added:,}个):", dropoff_path)


if __name__ == "__main__":
    main()
