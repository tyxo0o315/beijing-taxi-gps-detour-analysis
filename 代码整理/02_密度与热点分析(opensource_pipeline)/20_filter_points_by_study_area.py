r"""
用19号脚本产出的study_area_candidates.gpkg(persistence_count==7聚类出的128个候选
研究片区多边形)筛选某一天的GPS轨迹点(01_feature_engineering.py的输出features.csv)。

跟"直接丢弃候选区外的点"不一样，这里保留轨迹连续性：以trip_break分段为单位——
同一辆车相邻两点间隔不超过max-gap-sec(01号脚本默认1800s)就算同一段连续路段；
一段路段只要有任意一个点落在128个候选区的任意一个里，就把这一整段(含区外的点)
原样保留。这样输出里同一辆车的一段轨迹在时间上还是连续的，能看出车辆进出候选区
前后的完整运动过程，不会被简单空间裁剪切成一堆看不出运动方向的孤立点。

跟01/09脚本一样按taxi_id分块处理(数据本身按taxi_id+timestamp全局排序，一辆车
一天的点数有限，不需要跨块搬运复杂状态)。

新增输出字段:
    candidate_rank: 该点具体落在哪个候选区(对应gpkg里的rank字段)，不在任何候选区
                    内(包括"整段被保留但该点本身在区外"这种情况)时为-1
其余字段原样透传(含原有的trip_break/event_type等)。

空间判定用shapely 2.0的STRtree做批量查询(candidate_rank每个chunk只算一次，不是
每个taxi算一次)，128个小候选面加起来占北京总面积不到1%，STRtree的包围盒剪枝
比对128个面各自做一次全量向量化contains快得多。

用法:
    python 20_filter_points_by_study_area.py \
        --input 20170301_core_features.csv \
        --regions ../study_area_candidates.gpkg \
        --output 20170301_filtered_by_study_area.csv
"""
import argparse
import os
import sys
import time

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely import STRtree, points as shapely_points

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kde_utils import lonlat_to_utm

CHUNK_SIZE = 2_000_000
DTYPES = {
    "group_id": "object", "taxi_id": "object", "timestamp": "int64",
    "latitude": "float64", "longitude": "float64", "direction": "object",
    "occupied": "int8", "positioning_valid": "int8",
    "dt_sec": "object", "speed_gps_kmh": "object", "speed_reported_kmh": "object",
    "heading_delta_deg": "object", "trip_break": "int8", "bad_coord": "int8",
    "out_of_day": "int8", "event_type": "object", "is_outlier_drift": "int8",
}
OUT_COLUMNS = list(DTYPES) + ["candidate_rank"]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True, help="某一天的<日期>_core_features.csv")
    p.add_argument("--regions", required=True, help="19号脚本产出的study_area_candidates.gpkg")
    p.add_argument("--output", required=True)
    p.add_argument("--max-gap-sec", type=int, default=1800,
                   help="仅用于打印统计信息里的口径说明，实际分段直接复用输入数据里"
                        "已经算好的trip_break字段(01号脚本用的也是这个默认值)")
    return p.parse_args()


def load_regions(gpkg_path):
    gdf = gpd.read_file(gpkg_path).sort_values("rank").reset_index(drop=True)
    tree = STRtree(gdf.geometry.values)
    ranks = gdf["rank"].values.astype(np.int32)
    return tree, ranks


def assign_candidate_rank(x, y, tree, ranks):
    pts = shapely_points(x, y)
    query_idx, tree_idx = tree.query(pts, predicate="intersects")
    result = np.full(len(x), -1, dtype=np.int32)
    result[query_idx] = ranks[tree_idx]
    return result


def flush_taxi_block(rows_df, out_f, header_written):
    """rows_df: 单个taxi_id、按原文件顺序(=按timestamp排序)的完整数据块。
    按trip_break切分连续路段，逐段判断是否命中候选区，命中就整段写出。"""
    trip_break = rows_df["trip_break"].values.astype(bool)
    seg_id = np.cumsum(trip_break) - 1
    in_area = rows_df["candidate_rank"].values >= 0

    keep_mask = np.zeros(len(rows_df), dtype=bool)
    for sid in np.unique(seg_id):
        sel = seg_id == sid
        if in_area[sel].any():
            keep_mask |= sel

    kept = rows_df.loc[keep_mask, OUT_COLUMNS]
    if len(kept):
        kept.to_csv(out_f, mode="a", header=not header_written["done"], index=False)
        header_written["done"] = True
    return len(rows_df), int(keep_mask.sum())


def main():
    args = parse_args()
    tree, ranks = load_regions(args.regions)

    if os.path.exists(args.output):
        os.remove(args.output)

    header_written = {"done": False}
    total_in = total_kept = 0
    block_taxi = None
    block_rows = []

    t0 = time.time()
    with open(args.output, "a", newline="", encoding="utf-8-sig") as out_f:
        for chunk in pd.read_csv(args.input, dtype=DTYPES, chunksize=CHUNK_SIZE, keep_default_na=False):
            x, y = lonlat_to_utm(chunk["longitude"].values, chunk["latitude"].values)
            chunk["candidate_rank"] = assign_candidate_rank(x, y, tree, ranks)

            for taxi_id, group in chunk.groupby("taxi_id", sort=False):
                if block_taxi is not None and taxi_id != block_taxi:
                    block_df = pd.concat(block_rows, ignore_index=True) if len(block_rows) > 1 else block_rows[0]
                    n_in, n_kept = flush_taxi_block(block_df, out_f, header_written)
                    total_in += n_in
                    total_kept += n_kept
                    block_rows = []
                block_taxi = taxi_id
                block_rows.append(group)

            print(f"已读入 {total_in + sum(len(g) for g in block_rows):,} 行(含缓存中未落盘的当前车辆), "
                  f"已保留 {total_kept:,} 行, 耗时 {time.time()-t0:.1f}s")

        if block_rows:
            block_df = pd.concat(block_rows, ignore_index=True) if len(block_rows) > 1 else block_rows[0]
            n_in, n_kept = flush_taxi_block(block_df, out_f, header_written)
            total_in += n_in
            total_kept += n_kept

    print()
    print(f"完成. 输入 {total_in:,} 行, 保留(整段路段，含段内区外的点) {total_kept:,} 行 "
          f"({total_kept/max(total_in,1)*100:.2f}%), 耗时 {time.time()-t0:.1f}s")
    print("输出:", args.output)
    print("candidate_rank>=0的行才是真正落在候选区内的点，-1是'因为同段有其它点命中"
          "候选区而被一并保留的区外点'，需要严格空间子集的话额外筛candidate_rank>=0即可")


if __name__ == "__main__":
    main()
