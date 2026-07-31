r"""
把多个日期/年份各自跑出来的同一主题密度栅格(比如每年的kd_baseline_all.tif)堆叠成一个
"时空立方体"——空间维度是像元(跟ArcGIS Pro的hex网格/栅格格子对应)，时间维度是波段
(每个日期一个波段)，概念上对应ArcGIS Pro的"Create Space Time Cube By Aggregating
Points"产出的netCDF时空立方体，但这里用最朴素的多波段GeoTIFF实现，不需要额外的
netCDF/xarray依赖(这台机器上装scipy/h3时遇到过pip哈希校验失败，为了避免同样的
不确定性，这里也不引入新依赖)。

前提: 这套pipeline目前只有2017-03-01一天的数据，这个脚本现在还用不上——是为将来
"多个年份/日期都跑完01~09之后"准备的。如果以后拿到2018、2019...同一套字段结构的
数据，重复跑一遍00~09(注意01和05脚本的--date参数要跟着改)，每年产出一份独立的
output_<年份>/目录，再用这个脚本把同一个主题的栅格按年份堆起来。

时间维度目前是"年份/日期"这种粗粒度(一年一个波段)，不是ArcGIS时空立方体那种可以做
Mann-Kendall趋势检验、Emerging Hot Spot Analysis的完整时间序列格式——如果需要那些
分析方法，最直接的路径是把这里产出的多波段GeoTIFF导入ArcGIS Pro，用"Create Space
Time Cube From Multidimensional Raster"转成原生的时空立方体格式，之后就能用ArcGIS
自带的那些时空分析工具了(见本目录README.md里的ArcGIS Pro导入说明)。

用法:
    python 11_build_space_time_cube.py \
        --band 2017-03-01=output_2017/kd_baseline_all.tif \
        --band 2018-03-01=output_2018/kd_baseline_all.tif \
        --band 2019-03-01=output_2019/kd_baseline_all.tif \
        --out space_time_cube_baseline.tif

每个--band是"标签=栅格路径"，标签会写进对应波段的描述(GDAL Band Description)，
在QGIS/ArcGIS里能看到每个波段对应哪个日期。所有输入栅格必须形状/坐标系/仿射变换
完全一致(即用同样的--cell-size/--radius/研究区跑出来的)，否则会报错退出，不会
静默地把不同分辨率/范围的栅格错位堆在一起。
"""
import argparse
import sys
import numpy as np
import rasterio


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--band", action="append", required=True, metavar="LABEL=PATH",
                   help="标签=栅格路径，可以传多次，按传入顺序作为波段顺序(建议按时间先后传)")
    p.add_argument("--out", required=True, help="输出多波段GeoTIFF路径")
    return p.parse_args()


def main():
    args = parse_args()
    labels, paths = [], []
    for item in args.band:
        if "=" not in item:
            sys.exit(f"--band参数格式应为 标签=路径，收到: {item}")
        label, path = item.split("=", 1)
        labels.append(label)
        paths.append(path)

    ref_profile = None
    bands = []
    for label, path in zip(labels, paths):
        with rasterio.open(path) as ds:
            arr = ds.read(1)
            profile = {"width": ds.width, "height": ds.height,
                       "crs": ds.crs, "transform": ds.transform}
            if ref_profile is None:
                ref_profile = profile
                ref_path = path
            else:
                mismatches = [k for k in profile if profile[k] != ref_profile[k]]
                if mismatches:
                    sys.exit(
                        f"{path} 跟第一个输入 {ref_path} 在 {mismatches} 上不一致——"
                        f"必须用完全相同的--cell-size/--radius/研究区跑出来的栅格才能堆叠，"
                        f"否则像元会对不上。"
                    )
            bands.append(arr)
        print(f"已读入波段 [{label}] <- {path}")

    stack = np.stack(bands, axis=0).astype(np.float32)
    out_profile_meta = dict(
        driver="GTiff", height=ref_profile["height"], width=ref_profile["width"],
        count=len(bands), dtype="float32", crs=ref_profile["crs"],
        transform=ref_profile["transform"], compress="lzw", nodata=0.0,
    )
    with rasterio.open(args.out, "w", **out_profile_meta) as dst:
        dst.write(stack)
        for i, label in enumerate(labels, start=1):
            dst.set_band_description(i, label)

    print(f"完成. {len(bands)}个波段(时间步) -> {args.out}")
    print("波段顺序:", labels)


if __name__ == "__main__":
    main()
