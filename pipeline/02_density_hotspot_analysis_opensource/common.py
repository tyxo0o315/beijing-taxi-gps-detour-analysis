r"""
共用路径/常量/工具函数。所有路径都可以用环境变量覆盖，方便部署到云端服务器时
不用改代码——直接在跑之前 export/set 对应变量即可，不传就用当前目录下的默认值。
"""
import os
import math

# 用环境变量覆盖，默认假设脚本和数据文件放在同一目录下运行。
DATA_DIR = os.environ.get("TAXI_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
CORE_CSV = os.environ.get("TAXI_CORE_CSV", os.path.join(DATA_DIR, "20170301_core.csv"))
FEATURES_CSV = os.environ.get("TAXI_FEATURES_CSV", os.path.join(DATA_DIR, "20170301_core_features.csv"))
OUT_DIR = os.environ.get("TAXI_OUT_DIR", os.path.join(DATA_DIR, "output"))

WGS84_EPSG = 4326
# UTM zone 50N，覆盖东经114-120度，北京(约116.4E)落在带内，米制单位。
# 跟arcpy版pipeline用的PROJECTED_WKID=32650是同一个坐标系。
UTM50N_EPSG = 32650

# 研究区裁剪范围(WGS84经纬度)，跟arcpy版pipeline的_density_common.py完全一致。
STUDY_AREA_WGS84 = (115.80, 39.50, 117.22, 40.55)  # xmin, ymin, xmax, ymax

CELL_SIZE_M_DEFAULT = 150       # 输出栅格分辨率(米)
SEARCH_RADIUS_M_DEFAULT = 500   # 核密度带宽(米)


def ensure_out_dir():
    os.makedirs(OUT_DIR, exist_ok=True)
    return OUT_DIR


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def circular_heading_diff(a, b):
    d = abs(a - b) % 360
    return d if d <= 180 else 360 - d
