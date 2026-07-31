"""
=============================================================================
 步骤1：时间筛选 —— 保留早高峰 7:00-9:00（北京时间）的路径
 步骤2：空间匹配 —— 标记路径是否经过研究区域（点在区域内→1，否则→0）
=============================================================================
输入：
  - optimal_path_sets.gpkg   （路径数据，CRS: EPSG:32650）
  - valid_area_wgs84.gpkg     （研究区域，CRS: EPSG:4326，即WGS84经纬度）
输出：
  - filtered_paths.gpkg       （筛选+标记后的结果）
  - filtered_paths.csv        （同上，方便非GIS工具查看）
=============================================================================
"""

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point
import warnings
warnings.filterwarnings('ignore')

# =========================== 0. 配置参数 ===========================

PATH_FILE   = "optimal_path_sets.gpkg"      # 路径文件
AREA_FILE   = "valid_area_wgs84.gpkg"        # 研究区域文件
OUTPUT_GPKG = "filtered_paths.gpkg"          # 输出 GPKG
OUTPUT_CSV  = "filtered_paths.csv"           # 输出 CSV（可选）

MORNING_START_HOUR = 7   # 早高峰开始（北京时间）
MORNING_END_HOUR   = 9   # 早高峰结束（北京时间）

# =========================== 1. 读取数据 ===========================

print("=" * 60)
print("📂 读取路径数据...")
paths = gpd.read_file(PATH_FILE)
print(f"   总记录数: {len(paths):,}")
print(f"   CRS: {paths.crs}")
print(f"   列名: {list(paths.columns)}")
print(f"   几何类型: {paths.geometry.geom_type.unique()}")

print("\n📂 读取研究区域数据...")
study_area = gpd.read_file(AREA_FILE)
print(f"   要素数: {len(study_area):,}")
print(f"   CRS: {study_area.crs}")
print(f"   几何类型: {study_area.geometry.geom_type.unique()}")

# =========================== 2. 检查时间字段格式 ===========================

# 打印前几行看看 start_time / end_time 长什么样
print("\n📅 时间字段预览（前5行）:")
time_cols = ['start_time', 'end_time']
for col in time_cols:
    if col in paths.columns:
        print(f"\n  [{col}]")
        print(f"    dtype: {paths[col].dtype}")
        print(f"    示例值: {paths[col].iloc[:5].tolist()}")
    else:
        raise KeyError(f"❌ 列 '{col}' 不存在！可用列: {list(paths.columns)}")

# =========================== 3. 时间筛选 ===========================

def parse_to_beijing_hour(series: pd.Series) -> pd.Series:
    """
    将时间列统一解析为北京时间的小时数（0-23）。
    兼容三种常见格式：
      - Unix 时间戳（整数/浮点数，UTC 秒）
      - datetime 字符串（如 "2017-03-01 07:30:00"）
      - 已经是 datetime64 类型
    """
    # 情况1：已经是 datetime64
    if pd.api.types.is_datetime64_any_dtype(series):
        dt = series
        # 如果是 tz-aware，先统一到 Asia/Shanghai
        if dt.dt.tz is not None:
            dt = dt.dt.tz_convert('Asia/Shanghai')
        return dt.dt.hour

    # 情况2：数值型 → 当作 Unix 时间戳 (UTC)，转为北京时间
    if pd.api.types.is_numeric_dtype(series):
        # UTC 时间戳 → 加 8 小时 → 北京时间
        beijing_seconds = (series + 8 * 3600) % 86400
        return (beijing_seconds // 3600).astype(int)

    # 情况3：字符串 → 解析为 datetime
    dt = pd.to_datetime(series, errors='coerce')
    if dt.dt.tz is not None:
        dt = dt.dt.tz_convert('Asia/Shanghai')
    return dt.dt.hour

print("\n" + "=" * 60)
print("⏰ 时间筛选: 北京时间 7:00-9:00 ...")

# 解析开始时间的小时
start_hour = parse_to_beijing_hour(paths['start_time'])
end_hour   = parse_to_beijing_hour(paths['end_time'])

# 筛选条件：
# - 开始时间在 7:00-9:00 之间（早高峰出发）
# - 或者与早高峰有重叠（开始<9 且 结束>7）
# 这里采用"路径与早高峰有交集"的逻辑：
mask_morning = (start_hour < MORNING_END_HOUR) & (end_hour >= MORNING_START_HOUR)

print(f"   早高峰路径数: {mask_morning.sum():,} / {len(paths):,} "
      f"({mask_morning.mean():.1%})")

paths_morning = paths[mask_morning].copy()
print(f"   筛选后记录数: {len(paths_morning):,}")

# =========================== 4. 空间匹配 ===========================

print("\n" + "=" * 60)
print("🌍 空间匹配: 判断路径是否经过研究区域 ...")

# 4.1 统一坐标系：将研究区域从 WGS84 转到 EPSG:32650
print(f"   研究区域原始 CRS: {study_area.crs}")
print(f"   目标 CRS: {paths_morning.crs}")

study_area_proj = study_area.to_crs(paths_morning.crs)
print(f"   重投影后 CRS: {study_area_proj.crs}")

# 4.2 如果研究区域有多个多边形，可以合并为一个（可选）
#    合并后空间连接更快，但会丢失"属于哪个子区域"的信息
#    如果需要保留子区域 ID，请注释掉下面这行
if len(study_area_proj) > 1:
    print(f"   ⚠️  研究区域包含 {len(study_area_proj)} 个要素，将合并为单一多边形...")
    study_area_union = study_area_proj.dissolve()  # 合并所有面
else:
    study_area_union = study_area_proj

# 4.3 空间连接：left join + intersects
#     sjoin 会自动使用空间索引（RTree）加速
print("   执行空间连接 (sjoin with intersects)...")
joined = gpd.sjoin(
    paths_morning,                    # left: 路径（LineString）
    study_area_union[['geometry']],   # right: 研究区域（Polygon）
    how='left',                       # 保留所有路径，未匹配的填 NaN
    predicate='intersects'            # 路径与研究区域有任一交点即匹配
)

# 4.4 生成标记列
#     index_right 非 NaN → 路径与研究区域相交 → 标记为 1
joined['in_study_area'] = joined['index_right'].notna().astype(int)

# 清理临时列
if 'index_right' in joined.columns:
    joined = joined.drop(columns=['index_right'])

print(f"   在研究区域内的路径数: {joined['in_study_area'].sum():,} "
      f"({joined['in_study_area'].mean():.1%})")

# =========================== 5. 输出结果 ===========================

print("\n" + "=" * 60)
print("💾 保存结果...")

# 5.1 保存为 GPKG（保留几何列，GIS 工具可用）
joined.to_file(OUTPUT_GPKG, driver='GPKG')
print(f"   ✅ {OUTPUT_GPKG}")

# 5.2 保存为 CSV（可选，方便 Excel / Pandas 查看）
#     几何列转为 WKT 文本
joined_csv = joined.copy()
joined_csv['geometry_wkt'] = joined_csv.geometry.apply(lambda g: g.wkt if g else None)
joined_csv = joined_csv.drop(columns=['geometry'])
joined_csv.to_csv(OUTPUT_CSV, index=False)
print(f"   ✅ {OUTPUT_CSV}")

# =========================== 6. 汇总信息 ===========================

print("\n" + "=" * 60)
print("📊 处理汇总")
print(f"   原始路径总数:              {len(paths):>10,}")
print(f"   早高峰路径数 (7:00-9:00):   {len(paths_morning):>10,}")
print(f"   其中在研究区域内:           {joined['in_study_area'].sum():>10,}")
print(f"   其中不在研究区域内:         {(joined['in_study_area']==0).sum():>10,}")
print(f"\n   输出文件: {OUTPUT_GPKG}, {OUTPUT_CSV}")
print("=" * 60)