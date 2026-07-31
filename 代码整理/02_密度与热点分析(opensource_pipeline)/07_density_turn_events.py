r"""
(e) 转向/疑似变道事件密度。开源版，逻辑对应arcpy版07脚本。同样的精度说明适用：
采样频率是"路段级"，这里识别的是路口转弯/大幅调头一类的粗粒度代理指标，
不是车道级变道识别。

用法:
    python 07_density_turn_events.py
    python 07_density_turn_events.py --heading-threshold 60 --min-speed 8
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
    "heading_delta_deg": "float64",
}
USECOLS = list(DTYPES)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=FEATURES_CSV)
    p.add_argument("--heading-threshold", type=float, default=45.0)
    p.add_argument("--min-speed", type=float, default=5.0)
    p.add_argument("--min-dt-sec", type=int, default=8)
    p.add_argument("--cell-size", type=float, default=None)
    p.add_argument("--radius", type=float, default=None)
    p.add_argument("--no-weight", action="store_true")
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
            & (chunk["speed_gps_kmh"] >= args.min_speed)
            & (chunk["heading_delta_deg"] >= args.heading_threshold)
        ]
        if len(sub):
            x, y = lonlat_to_utm(sub["longitude"].values, sub["latitude"].values)
            w = None if args.no_weight else sub["heading_delta_deg"].values
            acc.add(x, y, weights=w)
        total += len(chunk)
        print(f"已处理 {total:,} 行, 耗时 {time.time()-t0:.1f}s")

    out_path = os.path.join(out_dir, "kd_turn_events.tif")
    acc.finalize(out_path)
    print(f"疑似转向事件密度(航向变化>={args.heading_threshold}度, 代理指标非车道级变道):", out_path)


if __name__ == "__main__":
    main()
