r"""
用省级/区县级行政区划矢量(province.shp/county.shp，2024年全国省市县三级行政区划数据，审图号
GS(2024)0650号)提取"点是否落在北京市行政边界内"、"点落在北京哪个区"，取代之前用一个
粗糙经纬度矩形框(STUDY_AREA_WGS84)筛点的做法——矩形框会把跑到河北/天津境内但仍在
矩形范围内的点也算进来，也会漏掉边界犬牙交错处矩形框外但确实属于北京的点，边界矢量
判断更准确。

用shapely 2.0+的contains_xy做向量化点在多边形判断(内部是C实现+可以配合
shapely.prepare()预处理加速重复查询)，千万级点也能跑得动，不需要逐点循环或者
geopandas.sjoin那种更重的空间连接。

区县级边界(county.shp)主要用途：OD矩阵/流向分析这类"格子对格子"的分析，如果用09/12脚本
默认的300米六边形做空间单元，绝大多数格子对之间只有0-1趟行程，统计上没有意义、
流向图也看不清。北京市16个区是更合适的聚合粒度——`assign_beijing_district()`就是
给OD分析脚本用的，把每个点分配到对应的区。
"""
import os
import numpy as np
import geopandas as gpd
import shapely

_DEFAULT_SHP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boundary", "province.shp")
BOUNDARY_SHP = os.environ.get("TAXI_BOUNDARY_SHP", _DEFAULT_SHP)

_DEFAULT_DISTRICT_SHP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boundary", "county.shp")
DISTRICT_SHP = os.environ.get("TAXI_DISTRICT_SHP", _DEFAULT_DISTRICT_SHP)

_beijing_polygon_cache = None
_beijing_districts_cache = None


def load_beijing_polygon(shp_path=None):
    """读省级行政区划矢量，筛出北京市(省代码110000，字段名兜底再试'省'='北京市')，
    多个面(有的省份数据是多部件)合并成一个polygon，并prepare()加速后续重复查询。"""
    global _beijing_polygon_cache
    if _beijing_polygon_cache is not None:
        return _beijing_polygon_cache

    path = shp_path or BOUNDARY_SHP
    gdf = gpd.read_file(path)

    sub = None
    for code_col in ("省代码", "省级代码", "PAC", "AREACODE"):
        if code_col in gdf.columns:
            sub = gdf[gdf[code_col].astype(str) == "110000"]
            if not sub.empty:
                break
    if sub is None or sub.empty:
        for name_col in ("省", "NAME", "name"):
            if name_col in gdf.columns:
                sub = gdf[gdf[name_col].astype(str).str.contains("北京")]
                if not sub.empty:
                    break
    if sub is None or sub.empty:
        raise ValueError(
            f"在 {path} 里没找到北京市对应的面(试过省代码=110000和名称包含'北京')，"
            f"实际字段有: {gdf.columns.tolist()}"
        )

    geom = shapely.unary_union(sub.geometry.values)
    shapely.prepare(geom)
    _beijing_polygon_cache = geom
    return geom


def beijing_mask(lon, lat, shp_path=None):
    """向量化返回布尔数组：每个(lon, lat)点是否落在北京市行政边界内(WGS84经纬度，
    跟province.shp的坐标系一致，不需要投影)。"""
    polygon = load_beijing_polygon(shp_path)
    return shapely.contains_xy(polygon, lon, lat)


def load_beijing_districts(shp_path=None):
    """读区县级行政区划矢量，筛出北京市16个区(市代码/省代码=110000)，返回
    [(区名, 区代码, prepared polygon), ...]列表，每个区一条，polygon已经
    shapely.prepare()过，供assign_beijing_district()反复查询用。"""
    global _beijing_districts_cache
    if _beijing_districts_cache is not None:
        return _beijing_districts_cache

    path = shp_path or DISTRICT_SHP
    gdf = gpd.read_file(path)

    sub = None
    for code_col in ("市代码", "省代码", "PAC", "AREACODE"):
        if code_col in gdf.columns:
            sub = gdf[gdf[code_col].astype(str) == "110000"]
            if not sub.empty:
                break
    if sub is None or sub.empty:
        raise ValueError(
            f"在 {path} 里没找到北京市下属的区(试过市代码/省代码=110000)，"
            f"实际字段有: {gdf.columns.tolist()}"
        )

    name_col = "县" if "县" in sub.columns else ("NAME" if "NAME" in sub.columns else sub.columns[0])
    code_col_final = "县代码" if "县代码" in sub.columns else sub.columns[1]

    districts = []
    for row in sub.itertuples():
        name = getattr(row, name_col)
        code = getattr(row, code_col_final)
        geom = row.geometry
        shapely.prepare(geom)
        districts.append((name, code, geom))
    _beijing_districts_cache = districts
    return districts


def assign_beijing_district(lon, lat, shp_path=None):
    """向量化把每个(lon, lat)点分配到北京市16个区之一。返回区名的numpy字符串数组，
    落在16个区范围外的点(比如漂到河北境内)标成'unknown'。

    实现: 16个区各自做一次shapely.contains_xy向量化查询(不是逐点循环)，16次
    对全量点数组的向量化调用，比对每个点做16次逐一判断快得多。"""
    districts = load_beijing_districts(shp_path)
    lon = np.asarray(lon)
    lat = np.asarray(lat)
    result = np.full(lon.shape, "unknown", dtype=object)
    for name, code, geom in districts:
        mask = shapely.contains_xy(geom, lon, lat)
        result[mask] = name
    return result
