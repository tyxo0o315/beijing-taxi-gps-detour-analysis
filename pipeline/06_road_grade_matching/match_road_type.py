r"""
把GPS点(csv)匹配到最近的路网线要素，取道路等级`type`属性，输出成带`road_type`
字段的点shapefile。

!! 使用前必须先改下面的 INPUT_CSV 变量(或者用 --input 参数)，指向你自己的GPS点
csv文件路径 !! 这份代码只带了路网数据(2017_Beijing_road.shp)，不带GPS点数据。

方法说明
--------
路网线本身没有宽度，最初讨论过"给路网按等级建缓冲区+按优先级擦除重叠部分+点在
多边形内判断"这个思路，可行但实现复杂、计算量大(千万级点+5万+条路网线的缓冲区
擦除)。这里改用**最近邻查询+最大距离阈值**：用shapely 2.0的`STRtree`(R树空间索引，
C实现)给每个点找最近的路网线段，如果距离超过`--max-distance`(默认30米)就不赋值——
效果上跟缓冲区要解决的问题(路网线没有宽度，需要一个容差范围)是等价的，但不需要
构造/裁剪缓冲区多边形，也不需要给不同道路等级定缓冲区宽度和优先级，更快更简单。

道路数据(随包附带`2017_Beijing_road.shp`)的`type`字段是4类简化道路等级：
城市支路 / 城市次干路 / 高架及快速路 / 城市主干路

时段标签
--------
按csv里的时间戳(unix秒，默认列名`timestamp`)算北京时间的小时，打上早高峰(7~9点)/
晚高峰(17~19点)/平峰(其它时间)这3类标签，写进`time_period`字段。用`--period`可以
只筛选输出某一个时段的点(不传就是全部时段都要，只是每行多一个标签列)。

shapefile格式限制
------------------
ESRI Shapefile单文件有2GB上限，且字段名最长10个字符(超过会被自动截断，如果你的
csv里有超过10字符的列名、又刚好截断后重名，需要自己先改列名再跑)。如果GPS点数量
很大(千万级)，脚本会自动按`--max-rows-per-shp`(默认200万行)分成多个.shp文件
(`_part2`、`_part3`...)，不会因为超过格式限制而写坏或报错。如果不想要shp格式的
限制，可以自己把最后的`.to_file(..., driver="ESRI Shapefile")`换成
`driver="GPKG"`，GeoPackage没有这些限制，QGIS/ArcGIS Pro都能直接打开。

用法:
    python match_road_type.py --input 我的GPS点.csv
    python match_road_type.py --input 我的GPS点.csv --output 结果.shp --max-distance 50
    python match_road_type.py --input 我的GPS点.csv --lat-col lat --lon-col lon --ts-col ts
    python match_road_type.py --input 我的GPS点.csv --period 早高峰   # 只要早高峰(7-9点)的点
"""
import argparse
import os
import sys
import time

import numpy as np
import pandas as pd
import geopandas as gpd
import shapely
from pyproj import Transformer

# !!! 改成你自己的GPS点csv路径 !!! 必须包含经纬度两列(默认列名latitude/longitude，
# 不一致的话用 --lat-col/--lon-col 指定，不需要改这个变量)
INPUT_CSV = r"改成你自己的GPS点csv文件路径.csv"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROAD_SHP = os.path.join(SCRIPT_DIR, "2017_Beijing_road.shp")

WGS84_EPSG = 4326
UTM50N_EPSG = 32650  # 北京所在UTM分带(米制)，用于计算真实距离阈值，跟数据本身的
                       # 经纬度坐标系(WGS84)不是一回事，仅在内部计算距离时使用


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", default=INPUT_CSV, help="GPS点csv路径，必须有经纬度列")
    p.add_argument("--output", default=None, help="输出shp路径，默认在输入文件名后加_with_road_type.shp")
    p.add_argument("--road-shp", default=DEFAULT_ROAD_SHP)
    p.add_argument("--lat-col", default="latitude")
    p.add_argument("--lon-col", default="longitude")
    p.add_argument("--ts-col", default="timestamp", help="时间戳列名(unix秒)，默认timestamp")
    p.add_argument("--period", choices=["早高峰", "晚高峰", "平峰", "all"], default="all",
                   help="只输出这个时段的点(早高峰7-9点/晚高峰17-19点/平峰其它时间)，"
                        "默认all表示不筛选、全部时段都要(每行仍会打上time_period标签)")
    p.add_argument("--max-distance", type=float, default=30.0,
                   help="点到最近路的距离超过这个值(米)就不赋值，默认30米。"
                        "阈值越大覆盖率越高，但路口密集处误配到相邻道路的风险也越高。")
    p.add_argument("--chunk-size", type=int, default=1_000_000)
    p.add_argument("--max-rows-per-shp", type=int, default=2_000_000,
                   help="单个shp文件最多写多少行，超过自动分文件，默认200万")
    p.add_argument("--limit", type=int, default=None, help="只处理前N行，测试用")
    return p.parse_args()


def classify_time_period(ts_seconds):
    """向量化按北京时间(UTC+8)的小时打时段标签。unix纪元(1970-01-01 00:00:00 UTC)
    正好是UTC整点，(ts//3600)%24直接就是UTC小时，不需要构造datetime对象逐行转换，
    千万级数据也能算得很快。"""
    utc_hour = (ts_seconds // 3600) % 24
    beijing_hour = (utc_hour + 8) % 24
    period = np.full(len(ts_seconds), "平峰", dtype=object)
    period[(beijing_hour >= 7) & (beijing_hour < 9)] = "早高峰"
    period[(beijing_hour >= 17) & (beijing_hour < 19)] = "晚高峰"
    return period


def load_road_network(shp_path):
    """.dbf常见是GBK编码但.cpg声明可能对不上，依次尝试几种编码。"""
    gdf = None
    for kwargs in ({}, {"encoding": "gbk"}, {"encoding": "gb18030"}):
        try:
            gdf = gpd.read_file(shp_path, **kwargs)
            break
        except Exception:
            continue
    if gdf is None:
        raise ValueError(f"读取 {shp_path} 失败，试过默认编码/gbk/gb18030都不行")
    if "type" not in gdf.columns:
        raise ValueError(f"{shp_path} 里没有type字段，实际字段: {gdf.columns.tolist()}")
    if gdf.crs is None:
        raise ValueError(f"{shp_path} 没有坐标系信息(.prj缺失或读取失败)")
    return gdf.to_crs(epsg=UTM50N_EPSG)


def main():
    args = parse_args()
    if not os.path.exists(args.input):
        sys.exit(f"找不到输入文件 {args.input} —— 记得把INPUT_CSV改成你自己的GPS点csv路径，"
                  f"或者用 --input 参数指定")

    out_path = args.output or (os.path.splitext(args.input)[0] + "_with_road_type.shp")
    out_base, out_ext = os.path.splitext(out_path)

    print(f"读取路网: {args.road_shp}")
    roads = load_road_network(args.road_shp)
    print(f"路网线段数: {len(roads):,}, 道路类型: {sorted(roads['type'].unique())}")

    tree = shapely.STRtree(roads.geometry.values)
    type_arr = roads["type"].values
    to_utm = Transformer.from_crs(f"EPSG:{WGS84_EPSG}", f"EPSG:{UTM50N_EPSG}", always_xy=True)

    t0 = time.time()
    total_read = 0
    total_kept = 0
    matched = 0
    part_idx = 0
    part_rows = 0
    part_frames = []

    def flush_part():
        nonlocal part_idx, part_rows, part_frames
        if not part_frames:
            return
        df_out = pd.concat(part_frames, ignore_index=True)
        part_path = out_path if part_idx == 0 else f"{out_base}_part{part_idx + 1}{out_ext}"
        gdf_out = gpd.GeoDataFrame(
            df_out.drop(columns=["_lon", "_lat"]),
            geometry=gpd.points_from_xy(df_out["_lon"], df_out["_lat"]),
            crs=f"EPSG:{WGS84_EPSG}",
        )
        gdf_out.to_file(part_path, driver="ESRI Shapefile", encoding="utf-8")
        print(f"  写出 {part_path}: {len(gdf_out):,} 行")
        part_idx += 1
        part_rows = 0
        part_frames = []

    for chunk in pd.read_csv(args.input, chunksize=args.chunk_size):
        if args.limit is not None and total_read >= args.limit:
            break
        if args.lon_col not in chunk.columns or args.lat_col not in chunk.columns:
            sys.exit(f"csv里没有找到经纬度列 '{args.lon_col}'/'{args.lat_col}'，"
                      f"实际列名: {chunk.columns.tolist()}，用--lat-col/--lon-col指定正确的列名")
        if args.ts_col not in chunk.columns:
            sys.exit(f"csv里没有找到时间戳列 '{args.ts_col}'，"
                      f"实际列名: {chunk.columns.tolist()}，用--ts-col指定正确的列名")

        total_read += len(chunk)
        time_period = classify_time_period(chunk[args.ts_col].values.astype(np.int64))
        if args.period != "all":
            keep = time_period == args.period
            if not keep.any():
                continue
            chunk = chunk[keep].reset_index(drop=True)
            time_period = time_period[keep]
        total_kept += len(chunk)

        lon = chunk[args.lon_col].values
        lat = chunk[args.lat_col].values
        x, y = to_utm.transform(lon, lat)
        points = shapely.points(x, y)

        road_type = np.full(len(chunk), "", dtype=object)
        idx_pairs = tree.query_nearest(points, max_distance=args.max_distance, all_matches=False)
        input_idx, tree_idx = idx_pairs[0], idx_pairs[1]
        road_type[input_idx] = type_arr[tree_idx]
        matched += len(input_idx)

        chunk = chunk.copy()
        chunk["road_type"] = road_type
        chunk["time_period"] = time_period
        chunk["_lon"] = lon
        chunk["_lat"] = lat
        part_frames.append(chunk)
        part_rows += len(chunk)

        if part_rows >= args.max_rows_per_shp:
            flush_part()

        print(f"已读取 {total_read:,} 行, 时段筛选后剩余 {total_kept:,} 行, "
              f"匹配到路网={matched:,} ({matched/max(total_kept,1)*100:.1f}%), "
              f"耗时 {time.time()-t0:.1f}s")

    flush_part()
    print(f"完成. 读取={total_read:,} 时段筛选后={total_kept:,} 匹配到路网={matched:,} "
          f"({matched/max(total_kept,1)*100:.1f}%) 耗时={time.time()-t0:.1f}s")
    print(f"输出: {out_path}" + ("(以及后续_partN文件，如果数据量超过了--max-rows-per-shp)" if part_idx > 1 else ""))


if __name__ == "__main__":
    main()
