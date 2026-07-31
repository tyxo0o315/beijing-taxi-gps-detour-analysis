r"""
不依赖arcpy/Spatial Analyst许可的核密度估计(KDE)实现，只用numpy+pyproj+rasterio。

跟ArcGIS Kernel Density的对应关系
--------------------------------
ArcGIS的KernelDensity内部也是"点先落到细网格，再跟一个核函数做卷积"，默认核函数是
quartic(双权重)核:
    k(d) = (3 / (pi * radius^2)) * (1 - (d/radius)^2)^2   , d < radius
    k(d) = 0                                               , d >= radius
这里直接复刻同一个核函数、同一个cell_size/search_radius语义，用FFT做卷积(数学上
跟直接卷积等价，只是快很多)。area_unit_scale_factor用"SQUARE_KILOMETERS"对应：
最终密度值单位是"每平方公里"，跟旧arcpy pipeline _density_common.py的默认设置一致。

不是逐位数值对齐ArcGIS的输出(边界像元处理、点到像元中心的分配方式等细节可能有
微小差异)，但核函数/带宽/像元大小完全一致，量级和空间格局应该是一致的。

用HistogramAccumulator分两步做，是为了能在读CSV分块(chunksize)时增量累加，不需要
把4900万行、17列全部同时读进内存——2D直方图的累加满足线性可加性，卷积放到最后
所有分块读完之后只做一次。
"""
import numpy as np
import rasterio
from rasterio.transform import Affine
from pyproj import Transformer

from common import STUDY_AREA_WGS84, UTM50N_EPSG, WGS84_EPSG, CELL_SIZE_M_DEFAULT, SEARCH_RADIUS_M_DEFAULT

_to_utm = Transformer.from_crs(f"EPSG:{WGS84_EPSG}", f"EPSG:{UTM50N_EPSG}", always_xy=True)

_study_extent_cache = None


def lonlat_to_utm(lon, lat):
    """向量化转经纬度(WGS84)到UTM50N米制坐标。lon/lat可以是numpy/pandas数组。"""
    x, y = _to_utm.transform(np.asarray(lon), np.asarray(lat))
    return x, y


def get_study_extent_utm():
    global _study_extent_cache
    if _study_extent_cache is not None:
        return _study_extent_cache
    xmin, ymin, xmax, ymax = STUDY_AREA_WGS84
    xs = [xmin, xmin, xmax, xmax]
    ys = [ymin, ymax, ymin, ymax]
    ux, uy = lonlat_to_utm(xs, ys)
    _study_extent_cache = (float(min(ux)), float(min(uy)), float(max(ux)), float(max(uy)))
    return _study_extent_cache


def _next_pow2(n):
    """不依赖scipy.fft.next_fast_len，退而求其次用2的下一个幂次(numpy.fft原生支持，
    没有scipy也能跑；不是理论最优的5-smooth长度，但对这个数据规模够快)。"""
    p = 1
    while p < n:
        p *= 2
    return p


def quartic_kernel(radius_cells):
    """离散化的quartic(双权重)核，中心归一化到能直接乘计数值。"""
    r = radius_cells
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    dist = np.sqrt(xx.astype(np.float64) ** 2 + yy.astype(np.float64) ** 2)
    k = np.zeros_like(dist)
    mask = dist < r
    k[mask] = (1 - (dist[mask] / r) ** 2) ** 2
    k *= 3.0 / (np.pi * r ** 2)
    return k


class HistogramAccumulator:
    """先把点(UTM米制坐标)累加成细网格2D直方图，读完所有数据分块后再一次性做
    FFT核卷积。cell_size/search_radius语义跟arcpy版run_kernel_density一致。"""

    def __init__(self, cell_size=None, search_radius=None, extent_utm=None):
        self.cell_size = cell_size or CELL_SIZE_M_DEFAULT
        self.search_radius = search_radius or SEARCH_RADIUS_M_DEFAULT
        xmin, ymin, xmax, ymax = extent_utm or get_study_extent_utm()
        pad = self.search_radius  # 缓冲圈，让研究区边界外的点也能贡献边界像元密度
        self.xmin_p = xmin - pad
        self.ymin_p = ymin - pad
        self.xmax_p = xmax + pad
        self.ymax_p = ymax + pad
        self.xmin, self.ymin, self.xmax, self.ymax = xmin, ymin, xmax, ymax
        self.n_cols = int(np.ceil((self.xmax_p - self.xmin_p) / self.cell_size))
        self.n_rows = int(np.ceil((self.ymax_p - self.ymin_p) / self.cell_size))
        self.hist = np.zeros((self.n_rows, self.n_cols), dtype=np.float64)
        self.n_points_added = 0

    def add(self, x, y, weights=None):
        """weights=None表示每点算1次(纯出现次数密度)；也可以传数值数组做加权。"""
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        valid = np.isfinite(x) & np.isfinite(y)
        valid &= (x >= self.xmin_p) & (x < self.xmax_p) & (y >= self.ymin_p) & (y < self.ymax_p)
        if not valid.any():
            return
        x, y = x[valid], y[valid]
        w = None
        if weights is not None:
            w = np.asarray(weights, dtype=np.float64)[valid]
            wvalid = np.isfinite(w)
            if not wvalid.all():
                x, y, w = x[wvalid], y[wvalid], w[wvalid]
        h, _, _ = np.histogram2d(
            y, x, bins=[self.n_rows, self.n_cols],
            range=[[self.ymin_p, self.ymax_p], [self.xmin_p, self.xmax_p]],
            weights=w,
        )
        self.hist += h
        self.n_points_added += len(x)

    def finalize(self, out_path=None):
        """做FFT核卷积、裁掉缓冲圈、换算成"每平方公里"密度，可选写出GeoTIFF。"""
        radius_cells = max(1, int(round(self.search_radius / self.cell_size)))
        kernel = quartic_kernel(radius_cells)

        fshape = [self.hist.shape[i] + kernel.shape[i] - 1 for i in range(2)]
        fast_len = [_next_pow2(s) for s in fshape]
        H = np.fft.rfft2(self.hist, fast_len)
        K = np.fft.rfft2(kernel, fast_len)
        conv = np.fft.irfft2(H * K, fast_len)[:fshape[0], :fshape[1]]
        start = [(kernel.shape[i] - 1) // 2 for i in range(2)]
        density = conv[start[0]:start[0] + self.hist.shape[0], start[1]:start[1] + self.hist.shape[1]]

        row0 = int(round((self.ymax_p - self.ymax) / self.cell_size))
        row1 = int(round((self.ymax_p - self.ymin) / self.cell_size))
        col0 = int(round((self.xmin - self.xmin_p) / self.cell_size))
        col1 = int(round((self.xmax - self.xmin_p) / self.cell_size))
        density = density[row0:row1, col0:col1]

        # np.histogram2d(y, x, ...)按y递增排行(行索引0=y最小=最南边)，但GeoTIFF的
        # 北向上约定是行索引0=y最大=最北边(仿射变换里的-cell_size就是按这个假设写的)。
        # 两者顺序正好相反，不翻转的话整张栅格会南北镜像颠倒——之前就是这里漏翻转，
        # 导致输出的地理位置和真实位置对不上(不是坐标平移偏移，是整体上下颠倒)。
        density = np.flipud(density)

        # FFT卷积会在数学上"本该精确为0"的像元(离任何点都超过search_radius)上留下
        # 极小的浮点噪声(实测量级在1e-16~1e-24，用单点做过验证：只放1个点，理论上
        # 只有核半径内那几十个像元该非零，但FFT卷积后有超过一半的像元显示"非零"，
        # 全是这种噪声)。这些噪声在ArcGIS Pro等工具里用默认拉伸渲染时会被当成"真实的
        # 极小密度值"，铺满全图变成大片黑白噪点，跟真正的稀疏数据没法区分。这里在
        # 换算单位之前先按峰值的相对比例clip掉，把这些噪声钉死成精确的0(跟GeoTIFF
        # 的nodata=0.0对得上，能被正确识别为"没有数据"而不是"极小的真实值")。
        noise_floor = density.max() * 1e-6 if density.max() > 0 else 0.0
        density[density < noise_floor] = 0.0

        # quartic公式按"每平方米"归一化，这里换算成"每平方公里"，
        # 对应arcpy版area_unit_scale_factor="SQUARE_KILOMETERS"。
        density = density * (1000.0 / self.cell_size) ** 2
        density = np.clip(density, 0, None).astype(np.float32)

        if out_path:
            transform = Affine(self.cell_size, 0, self.xmin, 0, -self.cell_size, self.ymax)
            with rasterio.open(
                out_path, "w", driver="GTiff",
                height=density.shape[0], width=density.shape[1],
                count=1, dtype=density.dtype,
                crs=f"EPSG:{UTM50N_EPSG}", transform=transform,
                compress="lzw", nodata=0.0,
            ) as dst:
                dst.write(density, 1)
        return density, out_path
