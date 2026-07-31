r"""
时空立方体(Gi*+Mann-Kendall)之外的一条更简单、更不容易出错的选区路线：
直接用7天的密度栅格(默认kd_baseline_all.tif)本身做"跨天持续性"统计，
不需要六边形聚合、不需要全局均值/标准差，因此不会重蹈09/12脚本那次"全局统计量
被离群值污染导致z分数全部趋零"的覆辙——每天的百分位排名只在该天内部计算，
天与天之间不共享任何统计量，最坏情况下某一天数据有问题也只会让那一天的结果
跑偏，不会传染给其它天或者拉低整体判断的置信度。

方法
----
对每一天的密度栅格，只在有效像元(值>0)范围内计算百分位排名，取前 --top-pct
(默认10%)最高密度的像元标记为"当天热点"。7天各自独立打标之后，逐像元累加
"多少天被标记为热点"，得到一个0~7的"持续性计数"栅格——7天全部是热点的地方，
就是最值得选为重点研究区域的候选区(类似ArcGIS Emerging Hot Spot Analysis里
"persistent hotspot"的直觉，但不需要Gi*/趋势检验，也就没有那些统计量被极端值
污染的风险)。

用法:
    python 18_persistent_hotspot_select.py \
        --days 20170301=path/to/day1/kd_baseline_all.tif \
        --days 20170302=path/to/day2/kd_baseline_all.tif \
        ... (7天各一次)
        --top-pct 10 \
        --out study_area_candidates.tif
"""
import argparse
import os
import sys

import numpy as np
import rasterio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ensure_out_dir


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--day", action="append", required=True,
                   help="格式 YYYYMMDD=/path/to/kd_baseline_all.tif，重复7次(或任意天数>=2)")
    p.add_argument("--top-pct", type=float, default=10.0,
                   help="每天取密度最高的百分之几作为当天热点，默认10.0")
    p.add_argument("--out", default=None, help="输出tif路径，默认放在TAXI_OUT_DIR下")
    return p.parse_args()


def main():
    args = parse_args()
    pairs = []
    for item in args.day:
        label, path = item.split("=", 1)
        pairs.append((label, path))

    ref_profile = None
    daily_masks = []
    daily_norm = []  # 每天min-max归一化后的密度，用于同时输出一个强度参考图
    labels = []

    for label, path in pairs:
        with rasterio.open(path) as ds:
            arr = ds.read(1).astype(np.float64)
            profile = ds.profile
        if ref_profile is None:
            ref_profile = profile
        else:
            if (profile["width"], profile["height"], str(profile["crs"])) != \
               (ref_profile["width"], ref_profile["height"], str(ref_profile["crs"])):
                raise ValueError(f"{label} 的栅格尺寸/坐标系跟前面几天对不上，检查是不是同一个研究区域生成的")

        valid = arr > 0
        n_valid = int(valid.sum())
        if n_valid == 0:
            print(f"[{label}] 警告: 全是0，这天没有有效密度值，跳过")
            daily_masks.append(np.zeros_like(arr, dtype=bool))
            daily_norm.append(np.zeros_like(arr, dtype=np.float64))
            labels.append(label)
            continue

        threshold = np.percentile(arr[valid], 100.0 - args.top_pct)
        mask = arr >= threshold
        mask &= valid
        daily_masks.append(mask)

        vmax = arr[valid].max()
        norm = np.where(valid, arr / vmax, 0.0)
        daily_norm.append(norm)
        labels.append(label)

        print(f"[{label}] 有效像元={n_valid:,}, top{args.top_pct:.0f}%阈值={threshold:.2f}, "
              f"标记为热点的像元数={int(mask.sum()):,}")

    n_days = len(pairs)
    persistence = np.zeros_like(daily_masks[0], dtype=np.int16)
    for m in daily_masks:
        persistence += m.astype(np.int16)

    mean_norm = np.mean(np.stack(daily_norm, axis=0), axis=0).astype(np.float32)

    out_dir = ensure_out_dir() if args.out is None else os.path.dirname(os.path.abspath(args.out))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    out_path = args.out or os.path.join(out_dir, "study_area_candidates.tif")

    profile = ref_profile.copy()
    profile.update(count=2, dtype="float32", nodata=-1.0, compress="lzw")

    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(persistence.astype(np.float32), 1)
        dst.write(mean_norm, 2)
        dst.set_band_description(1, f"persistence_count_of_{n_days}_days_top{args.top_pct:.0f}pct")
        dst.set_band_description(2, "mean_normalized_density_across_days")

    print()
    print("完成:", out_path)
    print(f"  波段1 persistence_count: 0~{n_days}, 值=N 表示{n_days}天里有N天该像元落在当天密度前{args.top_pct:.0f}%")
    print(f"  波段2 mean_normalized_density: 每天各自min-max归一化后的密度均值(0~1)，同一片区多天强度对比参考")
    print()
    print("选区建议(在GIS里对波段1做分类渲染即可直接看):")
    print(f"  persistence_count == {n_days}          -> 核心候选区(7天全部是热点，最稳)")
    print(f"  persistence_count in [{n_days-2}, {n_days-1}] -> 次级候选区(绝大多数天是热点)")
    print(f"  persistence_count <= {max(n_days-3,0)}          -> 不建议选(偶发/不稳定)")

    counts = np.bincount(persistence[persistence >= 0].astype(np.int64).ravel(), minlength=n_days + 1)
    print()
    print("persistence_count 值分布(像元数):")
    for k in range(n_days, -1, -1):
        print(f"  {k}: {counts[k]:,}")


if __name__ == "__main__":
    main()
