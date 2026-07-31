r"""
(g) 六边形网格聚合。开源版，不依赖arcpy(GenerateTessellation/SummarizeWithin)，
用hex_utils.py的向量化axial坐标绑定 + 分块流式累加实现。

比arcpy版多做了一件事：arcpy版09脚本在49.6M全量点上尝试过"每个六边形的点数/平均
GPS速度/平均航向变化"这个更完整的聚合(hex_stats_all)，但放弃了——因为按行取模分批
后用"mean_i * count_i"重建全局加权平均值，对存在大量NULL的字段(比如每辆车每段
行程的第一个点没有speed_gps_kmh)不成立，SummarizeWithin的MEAN会自动排除NULL但
Point_Count不排除，不同批次NULL占比不同导致重建出的加权平均有偏差。

这里是"分块累加sum和有效计数(逐字段各自的count，不共用一个总数)"，不是"分块算好
mean后再拿count加权重建"，所以没有这个偏差——每个字段的mean = 该字段sum / 该
字段自己的有效计数，跟一次性对全量数据算mean完全等价，不需要放弃这个统计量。

!! 重要bug修复(2026-07-23) !!：早期版本算mean_speed_gps_kmh/mean_heading_delta_deg
时没有排除`is_outlier_drift=1`的点，导致个别GPS漂移点算出的离谱速度值(实测真实
七天数据里出现过"平均速度"高达338万km/h的六边形，全是point_count=1~6的格子被
单个漂移点主导)直接拉爆了那个格子的平均值。这些离谱值混进多天时空聚类分析
(12_spacetime_clustering.py)的全局均值/标准差计算后，会让Getis-Ord Gi*的分母
被极端值撑到超大，所有格子的z分数都被压成0——实测跑出来的结果是mean_speed_gps_kmh
和mean_heading_delta_deg这两个字段的时空聚类**100%显示"no_pattern_detected"**，
点数类字段(point_count/pickup_count/dropoff_count，本身不可能出现这种离谱值)不
受影响、聚类结果是正常的。现在排除is_outlier_drift=1的点、并且对speed_gps_kmh
额外加200km/h的物理合理性上限(双重保险，防止漏网的漂移点)，再计算这两个mean字段。

用法:
    python 09_grid_aggregation.py
    python 09_grid_aggregation.py --hex-size 500
"""
import argparse
import os
import sys
import time
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import FEATURES_CSV, UTM50N_EPSG, ensure_out_dir
from kde_utils import lonlat_to_utm
from hex_utils import bin_points_to_hex, build_hex_geodataframe
from boundary_utils import beijing_mask

CHUNK_SIZE = 2_000_000
DTYPES = {
    "latitude": "float64", "longitude": "float64",
    "event_type": "object", "speed_gps_kmh": "float64", "heading_delta_deg": "float64",
    "is_outlier_drift": "int8",
}
USECOLS = list(DTYPES)
MAX_SANE_SPEED_KMH = 200.0  # 跟01阶段"GPS差分速度>200km/h"的标记阈值保持一致


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=FEATURES_CSV)
    p.add_argument("--hex-size", type=float, default=300.0)
    return p.parse_args()


def main():
    args = parse_args()
    out_dir = ensure_out_dir()

    # 累加器: (q, r) -> dict，逐字段各自维护sum/count，避免"mean加权重建"的偏差。
    acc = {}
    circumradius_holder = {}

    t0 = time.time()
    total = 0
    for chunk in pd.read_csv(args.input, usecols=USECOLS, dtype=DTYPES, chunksize=CHUNK_SIZE,
                              keep_default_na=False, na_values={"speed_gps_kmh": [""], "heading_delta_deg": [""]}):
        chunk = chunk[beijing_mask(chunk["longitude"].values, chunk["latitude"].values)]
        if not len(chunk):
            continue
        x, y = lonlat_to_utm(chunk["longitude"].values, chunk["latitude"].values)
        q, r, circumradius = bin_points_to_hex(x, y, args.hex_size)
        circumradius_holder["v"] = circumradius

        # 漂移点(is_outlier_drift=1)或物理上不合理的速度(>200km/h)不参与mean计算，
        # 置成NaN——下面groupby的sum/count会自动跳过NaN，point_count/pickup_count/
        # dropoff_count这几个不依赖speed/heading的字段不受影响，正常统计全部点。
        not_drift = chunk["is_outlier_drift"].values == 0
        speed = chunk["speed_gps_kmh"].values.copy()
        speed[~(not_drift & (speed <= MAX_SANE_SPEED_KMH))] = np.nan
        heading = chunk["heading_delta_deg"].values.copy()
        heading[~not_drift] = np.nan

        df = pd.DataFrame({
            "q": q, "r": r,
            "speed_gps_kmh": speed,
            "heading_delta_deg": heading,
            "is_pickup": (chunk["event_type"].values == "pickup").astype(np.int64),
            "is_dropoff": (chunk["event_type"].values == "dropoff").astype(np.int64),
        })
        grp = df.groupby(["q", "r"], sort=False).agg(
            point_count=("q", "size"),
            speed_sum=("speed_gps_kmh", "sum"),
            speed_n=("speed_gps_kmh", "count"),
            heading_sum=("heading_delta_deg", "sum"),
            heading_n=("heading_delta_deg", "count"),
            pickup_count=("is_pickup", "sum"),
            dropoff_count=("is_dropoff", "sum"),
        )
        for (qi, ri), row in grp.iterrows():
            d = acc.get((qi, ri))
            if d is None:
                d = acc[(qi, ri)] = {
                    "point_count": 0, "speed_sum": 0.0, "speed_n": 0,
                    "heading_sum": 0.0, "heading_n": 0,
                    "pickup_count": 0, "dropoff_count": 0,
                }
            d["point_count"] += int(row["point_count"])
            d["speed_sum"] += float(row["speed_sum"])
            d["speed_n"] += int(row["speed_n"])
            d["heading_sum"] += float(row["heading_sum"])
            d["heading_n"] += int(row["heading_n"])
            d["pickup_count"] += int(row["pickup_count"])
            d["dropoff_count"] += int(row["dropoff_count"])

        total += len(chunk)
        print(f"已处理 {total:,} 行, 六边形数(目前)={len(acc):,}, 耗时 {time.time()-t0:.1f}s")

    circumradius = circumradius_holder["v"]
    qr_stats = {}
    for (q, r), d in acc.items():
        qr_stats[(q, r)] = {
            "point_count": d["point_count"],
            "mean_speed_gps_kmh": (d["speed_sum"] / d["speed_n"]) if d["speed_n"] else None,
            "mean_heading_delta_deg": (d["heading_sum"] / d["heading_n"]) if d["heading_n"] else None,
            "pickup_count": d["pickup_count"],
            "dropoff_count": d["dropoff_count"],
        }

    gdf = build_hex_geodataframe(qr_stats, circumradius, UTM50N_EPSG)
    out_path = os.path.join(out_dir, "hex_stats_all.gpkg")
    gdf.to_file(out_path, driver="GPKG")
    print(f"完成. 六边形总数={len(gdf):,} 耗时={time.time()-t0:.1f}s")
    print("输出(含point_count/mean_speed_gps_kmh/mean_heading_delta_deg/pickup_count/dropoff_count):", out_path)


if __name__ == "__main__":
    main()
