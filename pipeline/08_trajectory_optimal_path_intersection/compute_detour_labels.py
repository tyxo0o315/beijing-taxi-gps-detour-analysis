"""
==========================================================================
 为原始轨迹添加绕行标签（detour = 1 或 0）
 逻辑：实际长度 >= 最短路径长度 * 1.5   → 1（绕行）
       实际长度 <  最短路径长度 * 1.5   → 0（穿行）
==========================================================================
输入：
  - filtered_paths.gpkg          (上一步输出，含早高峰+研究区域内最短路径)
  - 2017030{d}_trip_lines_filtered.gpkg  (d=1..7)
输出：
  - 2017030{d}_trip_lines_labeled.gpkg   (添加 detour 列)
==========================================================================
"""

import geopandas as gpd
import pandas as pd

# ======================== 配置（可调整） ========================
OPTIMAL_FILE = "filtered_paths.gpkg"                               # 最优路径文件
RAW_PATTERN = "2017030{day}_trip_lines_filtered.gpkg"              # 原始文件模板
OUT_PATTERN = "2017030{day}_trip_lines_labeled.gpkg"               # 输出文件模板

# ================================================================

# 1. 读取最优路径，构建最短路径字典（alt_rank==1）
optimal_gdf = gpd.read_file(OPTIMAL_FILE)
# 筛选严格最短路径
best_routes = optimal_gdf[optimal_gdf['alt_rank'] == 1][['route_id', 'dist_m']]
# 去重（一个 route_id 可能因多段路径重复，但不应存在；谨慎去重）
best_routes = best_routes.drop_duplicates(subset='route_id')
# 构建字典
best_dict = best_routes.set_index('route_id')['dist_m'].to_dict()
print(f"最短路径字典大小: {len(best_dict)}")

# 2. 逐天处理
for day in range(1, 8):
    raw_path = RAW_PATTERN.format(day=day)
    print(f"处理: {raw_path}")
    raw_gdf = gpd.read_file(raw_path)
    
    # 构造 route_id: 日期_trip_id
    # 注意：trip_id 可能是整数，转为字符串
    date_str = f"2017030{day}"
    raw_gdf['route_id'] = date_str + "_" + raw_gdf['trip_id'].astype(str)
    
    # 匹配长度字典
    raw_gdf['optimal_length'] = raw_gdf['route_id'].map(best_dict)
    
    # 标记匹配情况
    matched_mask = raw_gdf['optimal_length'].notna()
    print(f"  匹配到最优路径的行程: {matched_mask.sum()} / {len(raw_gdf)}")
    
    # 计算原始路径长度（必须用米制投影）
    # 将几何临时转换到 EPSG:32650 计算长度（注意：原始几何不变）
    raw_wgs84 = raw_gdf.geometry.values   # 原始几何数组
    # 创建临时 GeoSeries 并计算长度
    raw_gdf_m = gpd.GeoSeries(raw_wgs84, crs="EPSG:4326").to_crs("EPSG:32650")
    raw_gdf['actual_length_m'] = raw_gdf_m.length
    
    # 计算比值
    ratio = raw_gdf['actual_length_m'] / raw_gdf['optimal_length']
    # 生成标签（未匹配的设为 -1）
    raw_gdf['detour'] = -1
    raw_gdf.loc[matched_mask, 'detour'] = (ratio[matched_mask] >= 1.).astype(int)
    
    # 统计
    if matched_mask.sum() > 0:
        detour_rate = (raw_gdf.loc[matched_mask, 'detour'] == 1).mean() * 100
        print(f"  绕行率: {detour_rate:.1f}%")
    
    # 删除临时列（可选）
    # raw_gdf.drop(columns=['route_id', 'optimal_length', 'actual_length_m'], inplace=True)
    
    # 保存
    out_path = OUT_PATTERN.format(day=day)
    raw_gdf.to_file(out_path, driver='GPKG')
    print(f"  已保存: {out_path}\n")

print("全部完成！")