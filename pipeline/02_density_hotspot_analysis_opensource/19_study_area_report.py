r"""
把18_persistent_hotspot_select.py产出的study_area_candidates.tif(波段1=
persistence_count, 0~N天)转成一份"具体候选区域清单"：
- 对persistence_count>=--min-count的像元做连通域分析(4邻域)，一片连通区域
  就是一个候选研究片区，过滤掉太小的碎片(--min-cells)
- 每个候选片区算面积、几何中心(反投影回经纬度)、落在北京哪个区(assign_beijing_district)
- 按面积从大到小排序输出到控制台 + candidates.csv，同时另外输出一份矢量
  study_area_candidates.gpkg(每个候选区一个多边形，带rank/area_km2/district属性)，
  可以直接拖进GIS里跟其它图层对照，不需要再自己手动画研究区域范围。

用法:
    python 19_study_area_report.py --input study_area_candidates.tif --min-count 6
"""
import argparse
import os
import sys

import numpy as np
import rasterio
from rasterio.features import shapes as rio_shapes
import geopandas as gpd
import shapely
from pyproj import Transformer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from boundary_utils import assign_beijing_district

_NEIGHBOR_OFFSETS_8 = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def label_connected_components(mask):
    """纯numpy/纯Python的8邻域连通域标记，不依赖scipy(本机scipy装不上，pip哈希校验
    一直失败，历史遗留问题，pipeline其它脚本也是因为这个原因手写了连通域逻辑而不是
    用scipy.ndimage.label)。mask为True的像元数量通常远小于栅格总像元数(只有
    persistence_count>=min_count的那一小撮)，直接BFS足够快。"""
    h, w = mask.shape
    labels = np.zeros((h, w), dtype=np.int32)
    current_label = 0
    ys, xs = np.where(mask)
    for y0, x0 in zip(ys, xs):
        if labels[y0, x0] != 0:
            continue
        current_label += 1
        stack = [(y0, x0)]
        labels[y0, x0] = current_label
        while stack:
            y, x = stack.pop()
            for dy, dx in _NEIGHBOR_OFFSETS_8:
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and labels[ny, nx] == 0:
                    labels[ny, nx] = current_label
                    stack.append((ny, nx))
    return labels, current_label


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True, help="18号脚本产出的study_area_candidates.tif")
    p.add_argument("--min-count", type=int, default=6,
                   help="persistence_count>=这个值的像元才参与聚类，默认6(N天里至少6天是热点)")
    p.add_argument("--min-cells", type=int, default=5,
                   help="连通域小于这么多像元(150m格子)就当碎片丢弃，默认5(=0.1125km^2)")
    p.add_argument("--out-csv", default=None)
    p.add_argument("--out-gpkg", default=None)
    return p.parse_args()


def main():
    args = parse_args()
    in_dir = os.path.dirname(os.path.abspath(args.input))
    out_csv = args.out_csv or os.path.join(in_dir, "study_area_candidates.csv")
    out_gpkg = args.out_gpkg or os.path.join(in_dir, "study_area_candidates.gpkg")

    with rasterio.open(args.input) as ds:
        persistence = ds.read(1)
        mean_norm = ds.read(2) if ds.count >= 2 else None
        transform = ds.transform
        crs = ds.crs
        cell_area_km2 = abs(transform.a * transform.e) / 1e6

    mask = persistence >= args.min_count
    labeled, n_clusters = label_connected_components(mask)  # 8邻域，斜角也算相连
    print(f"min_count>={args.min_count} 的像元共 {int(mask.sum()):,} 个, 初步连通域 {n_clusters} 个")

    to_wgs84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)

    rows = []
    geoms = []
    for cluster_id in range(1, n_clusters + 1):
        cluster_mask = labeled == cluster_id
        n_cells = int(cluster_mask.sum())
        if n_cells < args.min_cells:
            continue

        ys, xs = np.where(cluster_mask)
        mean_persistence = float(persistence[cluster_mask].mean())
        mean_density = float(mean_norm[cluster_mask].mean()) if mean_norm is not None else None

        # 像元中心的地图坐标(UTM)，再转经纬度算几何中心
        xs_map = transform.c + (xs + 0.5) * transform.a
        ys_map = transform.f + (ys + 0.5) * transform.e
        cx_utm, cy_utm = xs_map.mean(), ys_map.mean()
        cx_lon, cy_lat = to_wgs84.transform(cx_utm, cy_utm)

        rows.append({
            "n_cells": n_cells,
            "area_km2": round(n_cells * cell_area_km2, 3),
            "mean_persistence": round(mean_persistence, 2),
            "mean_density_norm": round(mean_density, 3) if mean_density is not None else None,
            "center_lon": round(cx_lon, 5),
            "center_lat": round(cy_lat, 5),
        })

        # 用rasterio.features.shapes把这片连通域的像元转成多边形(可能有多个部件，合并)
        cluster_arr = cluster_mask.astype(np.uint8)
        polys = [shapely.geometry.shape(geom) for geom, val in rio_shapes(cluster_arr, transform=transform) if val == 1]
        geoms.append(shapely.unary_union(polys))

    if not rows:
        print("没有满足条件的候选区域，考虑调低--min-count或--min-cells")
        return

    order = sorted(range(len(rows)), key=lambda i: -rows[i]["area_km2"])
    rows = [rows[i] for i in order]
    geoms = [geoms[i] for i in order]

    centers_lon = np.array([r["center_lon"] for r in rows])
    centers_lat = np.array([r["center_lat"] for r in rows])
    districts = assign_beijing_district(centers_lon, centers_lat)
    for r, d in zip(rows, districts):
        r["district"] = d
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    gdf = gpd.GeoDataFrame(rows, geometry=geoms, crs=crs)
    gdf = gdf[["rank", "district", "area_km2", "n_cells", "mean_persistence",
               "mean_density_norm", "center_lon", "center_lat", "geometry"]]
    gdf.to_file(out_gpkg, driver="GPKG")

    import csv
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["rank", "district", "area_km2", "n_cells",
                                           "mean_persistence", "mean_density_norm",
                                           "center_lon", "center_lat"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in w.fieldnames})

    print()
    print(f"候选研究区域共 {len(rows)} 个 (已按面积从大到小排序):")
    print(f"{'rank':>4} {'district':<10} {'area_km2':>9} {'mean_persist':>13} {'center_lon':>11} {'center_lat':>11}")
    for r in rows:
        print(f"{r['rank']:>4} {r['district']:<10} {r['area_km2']:>9.2f} "
              f"{r['mean_persistence']:>13.2f} {r['center_lon']:>11.5f} {r['center_lat']:>11.5f}")

    print()
    print("输出:")
    print(" ", out_csv)
    print(" ", out_gpkg, "(每个候选区一个多边形，可直接拖进GIS查看)")


if __name__ == "__main__":
    main()
