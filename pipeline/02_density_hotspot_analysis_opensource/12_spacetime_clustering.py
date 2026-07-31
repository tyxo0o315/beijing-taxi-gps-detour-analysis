r"""
空间-时间聚类：把多天(比如7天)的六边形聚合结果(09_grid_aggregation.py每天产出的
hex_stats_all.gpkg)放在一起，识别"哪些地方是持续/新增/减弱的热点聚集区"，概念上
对应ArcGIS Pro"Emerging Hot Spot Analysis"(时空立方体上的涌现热点分析)，这里用
纯numpy/pandas从头实现(Getis-Ord Gi* + Mann-Kendall趋势检验都是不太复杂的统计量，
没有引入scipy/esda/pysal这些库——这台机器上装scipy/h3时遇到过pip哈希校验失败，
为避免同样的不确定性继续不依赖它们；数学上是标准公式，不是简化近似)。

方法
----
1. 多天的hex_stats_all按(grid_q, grid_r)对齐(09脚本的六边形axial坐标是基于UTM绝对
   坐标算的，不依赖当天数据的范围，所以不同天只要用同样的--hex-size，网格天然对齐，
   不需要重新做空间配准)
2. 每天单独算一次Getis-Ord Gi*局部统计量(每个六边形 vs 它的6个邻居+自身，跟当天
   全部六边形的均值/标准差比较，得到z分数——z越大代表这个格子和周围"扎堆偏高"，
   不是看它自己绝对值多高)
3. 每个格子把N天的Gi* z分数串成时间序列，做Mann-Kendall趋势检验(不依赖scipy，
   标准秩和公式手写实现)，判断这个格子的"热度"是在增强、减弱、还是没有显著趋势
4. 结合"显著热点天数"+"趋势"，把每个格子分类成几种模式(持久热点/增强热点/减弱热点/
   连续新增热点/零星热点/冷热振荡/无显著模式)，简化自ArcGIS的8分类体系，不是逐项
   照抄它的确切算法细节
5. 把"显著热点"的格子按六边形邻接关系做连通分量(Union-Find)，直接识别出**多个
   独立的聚集区**(而不是散落的单个格子)，这是回答"识别多个聚集区"这个问题的核心步骤

局限
----
- Getis-Ord Gi*和Mann-Kendall都是标准公式，但ArcGIS的Emerging Hot Spot Analysis
  还有一些实现细节(比如空间权重的具体构建方式、多重检验校正FDR)这里没有照搬，
  分类结果的量级/边界跟ArcGIS版可能对不上，用来看空间格局/相对排名足够，不要
  拿具体z值去跟ArcGIS的输出做逐格对比
- 7天(或更少)时间点做趋势检验统计功效有限，n越小结论越不sure，这里默认阈值
  |z|>1.96(约95%置信度)，可以用--alpha调

用法:
    python 12_spacetime_clustering.py \
        --day 2017-03-01=output_20170301/hex_stats_all.gpkg \
        --day 2017-03-02=output_20170302/hex_stats_all.gpkg \
        --day 2017-03-03=output_20170303/hex_stats_all.gpkg \
        --value-field point_count \
        --out space_time_clusters.gpkg

每个--day是"日期标签=当天hex_stats_all.gpkg路径"，可以传任意多天(2天以上就能跑，
天数越多趋势检验越有意义)。--value-field选用哪一列做聚集强度依据，默认point_count，
也可以传pickup_count/dropoff_count等hex_stats_all里的数值列。
"""
import argparse
import sys
import numpy as np
import pandas as pd
import geopandas as gpd

sys.path.insert(0, __file__.rsplit("/", 1)[0] if "/" in __file__ else __file__.rsplit("\\", 1)[0])
from hex_utils import hex_neighbors, hex_polygon, axial_to_center


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--day", action="append", required=True, metavar="LABEL=PATH",
                   help="日期标签=hex_stats_all.gpkg路径，按时间先后顺序传，至少传2天")
    p.add_argument("--value-field", default="point_count",
                   help="用hex_stats_all里的哪一列做聚集强度依据，默认point_count")
    p.add_argument("--alpha-z", type=float, default=1.96,
                   help="显著性阈值(|Gi* z分数|超过这个值算显著热/冷点)，默认1.96(~95%%置信度)")
    p.add_argument("--circumradius", type=float, default=None,
                   help="六边形外接半径(米)，不传就从第一天的hex_stats_all几何形状自动反推")
    p.add_argument("--out", default="space_time_clusters.gpkg")
    return p.parse_args()


def infer_circumradius(gdf):
    """从六边形的第一个几何形状反推外接半径(中心到顶点距离)。"""
    geom = gdf.geometry.iloc[0]
    cx, cy = geom.centroid.x, geom.centroid.y
    coords = list(geom.exterior.coords)[:-1]
    dists = [((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 for x, y in coords]
    return float(np.mean(dists))


def getis_ord_gi_star(value_by_qr, universe, universe_set):
    """标准Getis-Ord Gi*局部统计量(含自身的"*"变体)。
    value_by_qr: {(q,r): value}，universe: 参与本次计算的全部(q,r)有序列表(缺失值当0)，
    universe_set: 同样内容的set，专门给"in"成员判断用——千万别在list上做"in"查找，
    对近2万个六边形会是O(n^2)，实测直接从几秒钟变成几分钟卡死，这里踩过这个坑。
    返回 {(q,r): z分数}。"""
    n = len(universe)
    all_vals = np.array([value_by_qr.get(qr, 0.0) for qr in universe], dtype=np.float64)
    x_bar = all_vals.mean()
    s = all_vals.std()
    if s == 0:
        return {qr: 0.0 for qr in universe}

    z_scores = {}
    for qr in universe:
        neighbors = [qr] + [nb for nb in hex_neighbors(*qr) if nb in universe_set]
        w_sum = len(neighbors)  # 权重全是1(二元邻接)，w_ij平方还是w_ij
        x_sum = sum(value_by_qr.get(nb, 0.0) for nb in neighbors)
        numerator = x_sum - x_bar * w_sum
        denom = s * np.sqrt((n * w_sum - w_sum ** 2) / (n - 1))
        z_scores[qr] = numerator / denom if denom > 0 else 0.0
    return z_scores


def mann_kendall_z(series):
    """标准Mann-Kendall趋势检验(不含并列值校正，n较小时够用)。
    返回(S统计量, Z分数)。Z>0趋势上升，Z<0趋势下降。"""
    x = np.asarray(series, dtype=np.float64)
    n = len(x)
    s = 0
    for i in range(n - 1):
        s += np.sum(np.sign(x[i + 1:] - x[i]))
    var_s = n * (n - 1) * (2 * n + 5) / 18.0
    if var_s <= 0:
        return s, 0.0
    if s > 0:
        z = (s - 1) / np.sqrt(var_s)
    elif s < 0:
        z = (s + 1) / np.sqrt(var_s)
    else:
        z = 0.0
    return s, z


def classify_cell(gi_series, mk_z, alpha_z):
    sig_hot = gi_series > alpha_z
    sig_cold = gi_series < -alpha_z
    n_hot, n_cold, n = sig_hot.sum(), sig_cold.sum(), len(gi_series)

    if n_hot == 0 and n_cold == 0:
        return "no_pattern_detected"
    if n_hot > 0 and n_cold > 0:
        return "oscillating_hotspot"
    if n_hot == n:
        if mk_z > alpha_z:
            return "intensifying_hotspot"
        if mk_z < -alpha_z:
            return "diminishing_hotspot"
        return "persistent_hotspot"
    if n_cold == n:
        return "persistent_coldspot"
    if n_hot > 0:
        # 是否是"从某天开始一直连续到最后一天都是热点"(新增/连续热点)
        trailing = sig_hot[::-1]
        run = 0
        for v in trailing:
            if v:
                run += 1
            else:
                break
        if run == n_hot and run >= 2:
            return "consecutive_hotspot"
        return "sporadic_hotspot"
    return "sporadic_coldspot"


def connected_components(hot_cells):
    """六边形邻接关系上对"热点格子集合"做连通分量，找出多个独立的聚集区。"""
    hot_set = set(hot_cells)
    visited = set()
    clusters = []
    for start in hot_cells:
        if start in visited:
            continue
        stack = [start]
        comp = []
        visited.add(start)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb in hex_neighbors(*cur):
                if nb in hot_set and nb not in visited:
                    visited.add(nb)
                    stack.append(nb)
        clusters.append(comp)
    return clusters


def main():
    args = parse_args()
    labels, values_by_day = [], []
    circumradius = args.circumradius
    crs_epsg = None

    for item in args.day:
        if "=" not in item:
            sys.exit(f"--day参数格式应为 标签=路径，收到: {item}")
        label, path = item.split("=", 1)
        gdf = gpd.read_file(path)
        if args.value_field not in gdf.columns:
            sys.exit(f"{path} 里没有列 '{args.value_field}'，实际列: {gdf.columns.tolist()}")
        if circumradius is None:
            circumradius = infer_circumradius(gdf)
        if crs_epsg is None:
            crs_epsg = gdf.crs.to_epsg()
        # 注意: `x or 0.0`对NaN不安全——NaN在Python里是"真值"，`nan or 0.0`结果还是nan，
        # 不会被换成0.0。像mean_speed_gps_kmh这种字段，某个格子如果没有任何有效速度
        # 样本，源头会存None/NaN，读进gpkg后经常被序列化成NaN，混进下面的全局均值/
        # 标准差计算会导致NaN污染整个数组，从而让Gi*分母变成NaN、所有格子的z分数被
        # 静默压成0(实测point_count/pickup_count这类不会出现NaN的字段完全正常，
        # mean_speed_gps_kmh这类偶尔有缺失的字段就会精确复现这个"全部no_pattern"的
        # 现象)。显式判断NaN，不依赖`or`的真值判断。
        def _safe_float(v):
            if v is None:
                return 0.0
            fv = float(v)
            return 0.0 if np.isnan(fv) else fv

        value_by_qr = {
            (int(row.grid_q), int(row.grid_r)): _safe_float(getattr(row, args.value_field))
            for row in gdf.itertuples()
        }
        labels.append(label)
        values_by_day.append(value_by_qr)
        print(f"[{label}] <- {path}: {len(value_by_qr):,} 个六边形有数据")

    if len(labels) < 2:
        sys.exit("至少需要传2天(--day)才能做时空趋势分析")

    universe_set = set()
    for vbd in values_by_day:
        universe_set |= set(vbd.keys())
    universe = sorted(universe_set)
    print(f"合并后总六边形数(至少1天有数据): {len(universe):,}")

    print("逐天计算Getis-Ord Gi*局部统计量...")
    gi_by_day = []
    for label, vbd in zip(labels, values_by_day):
        gi_by_day.append(getis_ord_gi_star(vbd, universe, universe_set))
        print(f"  [{label}] 完成")

    gi_matrix = np.array([[gi_by_day[d].get(qr, 0.0) for d in range(len(labels))] for qr in universe])

    print("逐格计算Mann-Kendall趋势 + 分类...")
    categories = []
    mk_zs = []
    for i in range(len(universe)):
        s, z = mann_kendall_z(gi_matrix[i])
        mk_zs.append(z)
        categories.append(classify_cell(gi_matrix[i], z, args.alpha_z))
    categories = np.array(categories)
    mk_zs = np.array(mk_zs)

    print("按分类统计:")
    for cat, cnt in pd.Series(categories).value_counts().items():
        print(f"  {cat}: {cnt:,}")

    # 用"最后一天显著热点"识别当前的独立聚集区(连通分量)
    last_day_gi = gi_matrix[:, -1]
    hot_cells_last_day = [qr for qr, g in zip(universe, last_day_gi) if g > args.alpha_z]
    clusters = connected_components(hot_cells_last_day)
    clusters.sort(key=len, reverse=True)
    print(f"\n最后一天({labels[-1]})识别出 {len(clusters)} 个独立聚集区(按格子数从大到小):")
    for i, comp in enumerate(clusters[:20]):
        print(f"  聚集区#{i+1}: {len(comp)} 个六边形格子")

    cluster_id_of = {}
    for i, comp in enumerate(clusters):
        for qr in comp:
            cluster_id_of[qr] = i + 1

    rows = []
    for idx, qr in enumerate(universe):
        cx, cy = axial_to_center(qr[0], qr[1], circumradius)
        row = {
            "grid_q": qr[0], "grid_r": qr[1],
            "geometry": hex_polygon(cx, cy, circumradius),
            "mk_trend_z": mk_zs[idx],
            "category": categories[idx],
            "cluster_id_last_day": cluster_id_of.get(qr, 0),
        }
        for d, label in enumerate(labels):
            row[f"value_{label}"] = values_by_day[d].get(qr, 0.0)
            row[f"gi_star_{label}"] = gi_matrix[idx, d]
        rows.append(row)

    out_gdf = gpd.GeoDataFrame(rows, crs=f"EPSG:{crs_epsg}")
    out_gdf.to_file(args.out, driver="GPKG")
    print(f"\n完成. 输出: {args.out}")
    print("字段: grid_q/grid_r, value_<日期>(每天原始值), gi_star_<日期>(每天热点z分数), "
          "mk_trend_z(趋势检验), category(模式分类), cluster_id_last_day(最后一天的聚集区编号，0=不属于任何聚集区)")


if __name__ == "__main__":
    main()
