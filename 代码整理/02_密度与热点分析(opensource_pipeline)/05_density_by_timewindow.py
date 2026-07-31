r"""
(c) 分时段密度 —— 早高峰/晚高峰/平峰/夜间四个时间窗。开源版，逻辑对应arcpy版05脚本。

用法:
    python 05_density_by_timewindow.py
    python 05_density_by_timewindow.py --window morning_peak
"""
import argparse
import datetime
import os
import sys
import time
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import FEATURES_CSV, ensure_out_dir
from kde_utils import HistogramAccumulator, lonlat_to_utm
from boundary_utils import beijing_mask

CHUNK_SIZE = 2_000_000
DTYPES = {"latitude": "float64", "longitude": "float64", "timestamp": "int64"}
USECOLS = ["latitude", "longitude", "timestamp"]

TIME_WINDOWS = {
    "morning_peak": (7, 9),
    "evening_peak": (17, 19),
    "off_peak": (9, 17),
    "night": (0, 6),
}
_CST = datetime.timezone(datetime.timedelta(hours=8))


def epoch_range_for_hour_window(start_hour, end_hour, date_str):
    y, m, d = (int(v) for v in date_str.split("-"))
    start = datetime.datetime(y, m, d, start_hour, 0, 0, tzinfo=_CST)
    end = datetime.datetime(y, m, d, end_hour, 0, 0, tzinfo=_CST)
    return int(start.timestamp()), int(end.timestamp())


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=FEATURES_CSV)
    p.add_argument("--cell-size", type=float, default=None)
    p.add_argument("--radius", type=float, default=None)
    p.add_argument("--window", choices=list(TIME_WINDOWS), default=None)
    p.add_argument("--date", default="2017-03-01",
                   help="数据对应的日期(YYYY-MM-DD，北京时间)，换年份/日期时改这个参数即可")
    return p.parse_args()


def main():
    args = parse_args()
    out_dir = ensure_out_dir()
    windows = {args.window: TIME_WINDOWS[args.window]} if args.window else TIME_WINDOWS
    ranges = {name: epoch_range_for_hour_window(*hrs, args.date) for name, hrs in windows.items()}
    accs = {name: HistogramAccumulator(cell_size=args.cell_size, search_radius=args.radius)
            for name in windows}

    t0 = time.time()
    total = 0
    for chunk in pd.read_csv(args.input, usecols=USECOLS, dtype=DTYPES, chunksize=CHUNK_SIZE):
        chunk = chunk[beijing_mask(chunk["longitude"].values, chunk["latitude"].values)]
        for name, (t0_ts, t1_ts) in ranges.items():
            sub = chunk[(chunk["timestamp"] >= t0_ts) & (chunk["timestamp"] < t1_ts)]
            if len(sub):
                x, y = lonlat_to_utm(sub["longitude"].values, sub["latitude"].values)
                accs[name].add(x, y)
        total += len(chunk)
        print(f"已处理 {total:,} 行, 耗时 {time.time()-t0:.1f}s")

    for name, (h0, h1) in windows.items():
        out_path = os.path.join(out_dir, f"kd_time_{name}.tif")
        accs[name].finalize(out_path)
        print(f"{name} ({h0}-{h1}时) 密度:", out_path)


if __name__ == "__main__":
    main()
