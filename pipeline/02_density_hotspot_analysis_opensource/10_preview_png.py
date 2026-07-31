r"""
把 output/ 目录下的所有栅格(kd_*.tif)和六边形聚合(hex_stats_all.gpkg)快速渲染成PNG，
不需要装QGIS/ArcGIS就能看结果——在没有GIS软件的云端服务器上跑完这套pipeline后，
把output/整个目录(或者只挑这些PNG，体积小很多)传回本地用图片查看器直接看即可。

密度值分布通常高度右偏(极少数像元密度很高，大部分接近0)，默认用log1p做色阶拉伸，
不然大部分区域会因为跟热点比例悬殊而看起来"一片死黑"。

用法:
    python 10_preview_png.py                 # 处理output/下所有tif+gpkg
    python 10_preview_png.py --linear         # 不用log拉伸，线性色阶
"""
import argparse
import glob
import os
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import rasterio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import OUT_DIR


BG_COLOR = "#e8e8e8"  # 空白区域用浅灰底色，不是黑色——"没有数据"和"密度很低"要看得出区别


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", default=OUT_DIR)
    p.add_argument("--linear", action="store_true", help="不用log1p拉伸，直接线性色阶")
    p.add_argument("--cmap", default="turbo", help="matplotlib颜色映射名，默认turbo(蓝->红，"
                   "热点一眼能看出来，比inferno的深色端更容易跟'空白背景'区分)")
    p.add_argument("--min-percentile", type=float, default=25.0,
                   help="低于这个百分位的非零像元视为孤立噪声(比如郊区/山区偶尔一个出租车"
                        "GPS点)，只影响这张预览图的显示，不改GeoTIFF原始数值。设成0就什么"
                        "都不过滤。")
    return p.parse_args()


def preview_raster(tif_path, png_path, linear, cmap, min_percentile):
    with rasterio.open(tif_path) as ds:
        arr = ds.read(1).astype(np.float64)
        nodata = ds.nodata
    if nodata is not None:
        arr = np.where(arr == nodata, np.nan, arr)
    valid = np.isfinite(arr) & (arr > 0)
    if not valid.any():
        print(f"  跳过 {os.path.basename(tif_path)}：没有有效像元")
        return
    if min_percentile > 0:
        floor = np.percentile(arr[valid], min_percentile)
        valid = valid & (arr >= floor)
    disp = np.where(valid, (arr if linear else np.log1p(arr)), np.nan)

    cmap_obj = plt.get_cmap(cmap).copy()
    cmap_obj.set_bad(BG_COLOR)  # 密度=0/无数据的像元统一显示成浅灰，不落进色阶(不会显示成黑色)

    fig, ax = plt.subplots(figsize=(8, 8))
    ax.set_facecolor(BG_COLOR)
    im = ax.imshow(disp, cmap=cmap_obj)
    ax.set_title(os.path.basename(tif_path) +
                 ("" if linear else "  (log1p-stretched for display only, raster values unchanged)"))
    ax.set_xticks([]); ax.set_yticks([])
    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("log1p(density per sq.km)" if not linear else "density (per sq.km)")
    fig.tight_layout()
    fig.savefig(png_path, dpi=150, facecolor=BG_COLOR)
    plt.close(fig)
    print(f"  {os.path.basename(tif_path)} -> {png_path} "
          f"(有效像元范围 {np.nanmin(arr[valid]):.2f} ~ {np.nanmax(arr):.2f})")


def preview_hex(gpkg_path, out_dir, linear, cmap):
    import geopandas as gpd
    gdf = gpd.read_file(gpkg_path)
    cols = [c for c in ["point_count", "mean_speed_gps_kmh", "mean_heading_delta_deg",
                         "pickup_count", "dropoff_count"] if c in gdf.columns]
    for col in cols:
        fig, ax = plt.subplots(figsize=(8, 8))
        ax.set_facecolor(BG_COLOR)
        vals = gdf[col]
        plot_vals = vals if (linear or col.startswith("mean_")) else np.log1p(vals.clip(lower=0))
        gdf.assign(_v=plot_vals).plot(column="_v", cmap=cmap, ax=ax, legend=True,
                                        legend_kwds={"shrink": 0.7}, edgecolor="none")
        ax.set_title(f"hex_stats_all: {col}" +
                     ("" if (linear or col.startswith("mean_")) else " (log1p-stretched)"))
        ax.set_xticks([]); ax.set_yticks([])
        fig.tight_layout()
        out_path = os.path.join(out_dir, f"hex_{col}.png")
        fig.savefig(out_path, dpi=150, facecolor=BG_COLOR)
        plt.close(fig)
        print(f"  hex[{col}] -> {out_path}")


def main():
    args = parse_args()
    if not os.path.isdir(args.out_dir):
        raise FileNotFoundError(f"找不到输出目录 {args.out_dir}，先跑03~09")

    tifs = sorted(glob.glob(os.path.join(args.out_dir, "*.tif")))
    print(f"找到 {len(tifs)} 个栅格文件")
    for tif in tifs:
        png = os.path.splitext(tif)[0] + ".png"
        preview_raster(tif, png, args.linear, args.cmap, args.min_percentile)

    gpkg = os.path.join(args.out_dir, "hex_stats_all.gpkg")
    if os.path.exists(gpkg):
        print("渲染 hex_stats_all.gpkg ...")
        preview_hex(gpkg, args.out_dir, args.linear, args.cmap)
    else:
        print("未找到hex_stats_all.gpkg，跳过(先跑09_grid_aggregation.py)")


if __name__ == "__main__":
    main()
