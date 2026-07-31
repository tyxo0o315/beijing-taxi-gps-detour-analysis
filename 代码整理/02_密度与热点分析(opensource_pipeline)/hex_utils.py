r"""
不依赖arcpy(GenerateTessellation/SummarizeWithin)的六边形网格聚合，只用numpy+shapely+
geopandas。对千万级点用向量化的"axial坐标取整"直接算每个点落在哪个六边形里，
不逐点做空间连接(那样对49.7M点会慢到不可用)。

跟arcpy版09_grid_aggregation.py的hex_size语义对齐：--hex-size传入的值跟arcpy的
GenerateTessellation(..., f"{hex_size*hex_size} SquareMeters", ...)一样，是
"六边形面积=hex_size^2平方米"，不是边长。
"""
import numpy as np
import math
from shapely.geometry import Polygon
import geopandas as gpd

SQRT3 = math.sqrt(3.0)


def hex_size_to_circumradius(hex_size):
    """hex_size^2平方米 -> 正六边形外接半径(中心到顶点距离)，单位米。
    正六边形面积 = (3*sqrt(3)/2) * R^2 => R = sqrt(area / (3*sqrt(3)/2))。"""
    area = hex_size * hex_size
    return math.sqrt(area / (1.5 * SQRT3))


def points_to_axial(x, y, circumradius):
    """尖顶(pointy-top)六边形网格，向量化把笛卡尔坐标转成分数axial坐标(q, r)。
    标准公式，见redblobgames.com/grids/hexagons的axial坐标转换。"""
    r_ = circumradius
    q = (SQRT3 / 3.0 * x - 1.0 / 3.0 * y) / r_
    rr = (2.0 / 3.0 * y) / r_
    return q, rr


def axial_round(q, r):
    """分数axial坐标 -> 最近的整数六边形坐标(cube坐标取整再转回axial，标准算法)。"""
    x = q
    z = r
    y = -x - z
    rx, ry, rz = np.round(x), np.round(y), np.round(z)
    dx, dy, dz = np.abs(rx - x), np.abs(ry - y), np.abs(rz - z)

    x_gt_y = dx > dy
    x_gt_z = dx > dz
    y_gt_z = dy > dz

    fix_x = x_gt_y & x_gt_z
    fix_y = (~fix_x) & y_gt_z
    # 其余情况fix z

    rx = np.where(fix_x, -ry - rz, rx)
    ry = np.where((~fix_x) & fix_y, -rx - rz, ry)
    rz = np.where((~fix_x) & (~fix_y), -rx - ry, rz)
    return rx.astype(np.int64), rz.astype(np.int64)  # (q, r)


def axial_to_center(q, r, circumradius):
    """整数axial坐标 -> 该六边形中心点的笛卡尔坐标。"""
    x = circumradius * (SQRT3 * q + SQRT3 / 2.0 * r)
    y = circumradius * (1.5 * r)
    return x, y


def hex_polygon(cx, cy, circumradius):
    """尖顶六边形，顶点角度从30度开始每60度一个顶点。"""
    pts = []
    for k in range(6):
        angle = math.radians(60 * k + 30)
        pts.append((cx + circumradius * math.cos(angle), cy + circumradius * math.sin(angle)))
    return Polygon(pts)


AXIAL_NEIGHBOR_OFFSETS = [(1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1)]


def hex_neighbors(q, r):
    """标准axial坐标(cube x=q,z=r,y=-x-z)的6个相邻六边形坐标，顺序无所谓。"""
    return [(q + dq, r + dr) for dq, dr in AXIAL_NEIGHBOR_OFFSETS]


def bin_points_to_hex(x, y, hex_size):
    """向量化地把点(x,y，UTM米制)分配到六边形网格里。返回(q, r)整数数组。"""
    circumradius = hex_size_to_circumradius(hex_size)
    q_frac, r_frac = points_to_axial(np.asarray(x), np.asarray(y), circumradius)
    q, r = axial_round(q_frac, r_frac)
    return q, r, circumradius


def build_hex_geodataframe(qr_counts, circumradius, crs_epsg, count_col="point_count"):
    """qr_counts: {(q, r): count} 或 {(q, r): {列名: 值, ...}}。
    返回geopandas.GeoDataFrame，每行一个六边形，字段是grid_q/grid_r + 传入的统计列。"""
    rows = []
    for (q, r), val in qr_counts.items():
        cx, cy = axial_to_center(q, r, circumradius)
        poly = hex_polygon(cx, cy, circumradius)
        if isinstance(val, dict):
            row = {"grid_q": q, "grid_r": r, "geometry": poly, **val}
        else:
            row = {"grid_q": q, "grid_r": r, "geometry": poly, count_col: val}
        rows.append(row)
    return gpd.GeoDataFrame(rows, crs=f"EPSG:{crs_epsg}")
