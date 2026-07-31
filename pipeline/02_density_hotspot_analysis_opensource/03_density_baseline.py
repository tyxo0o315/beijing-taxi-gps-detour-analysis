r"""
(a) 全量点核密度 —— 活动强度基线。开源版，不依赖arcpy/Spatial Analyst许可，
只用numpy(FFT做核卷积)+pandas(分块读取，不用一次性把4900万行全读进内存)+rasterio
(写GeoTIFF)。核函数/cell_size/search_radius跟arcpy版语义完全一致，见kde_utils.py
顶部的说明。

用北京市行政边界矢量(boundary/province.shp)做点在多边形判断筛点，取代粗糙的经纬度矩形框，
见boundary_utils.py顶部说明。

用法:
    python 03_density_baseline.py
    python 03_density_baseline.py --cell-size 30 --radius 300
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
DTYPES = {"latitude": "float64", "longitude": "float64"}
USECOLS = ["latitude", "longitude"]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=FEATURES_CSV)
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
        chunk = chunk[beijing_mask(chunk["longitude"].values, chunk["latitude"].values)]
        x, y = lonlat_to_utm(chunk["longitude"].values, chunk["latitude"].values)
        acc.add(x, y)
        total += len(chunk)
        print(f"已处理 {total:,} 行, 耗时 {time.time()-t0:.1f}s")

    out_path = os.path.join(out_dir, "kd_baseline_all.tif")
    _, path = acc.finalize(out_path)
    print(f"完成. 累计点数={acc.n_points_added:,} 耗时={time.time()-t0:.1f}s")
    print("输出:", path)


if __name__ == "__main__":
    main()
