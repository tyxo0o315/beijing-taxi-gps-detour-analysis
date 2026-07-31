# 最优路径

给 `05_出租车拥堵OD提取流水线` 产出的每一对起讫点（OD）计算路网最短路径，并额外生成一条长度不超过最短路 120% 的备选路径，供 `08_OD轨迹线集与最优路径集求交算法` 判断实际行程是否绕行。

流程：**道路Shapefile构建有向图 → OD吸附到路网 → 最短路(Dijkstra) → 120%以内备选路 → 合并导出GPKG**

## 方法

- 只保留"可通行"的道路等级（`motorway`/`trunk`/`primary`/`secondary`/`tertiary`/`residential`/`unclassified`/`living_street`/`service` 及其 `_link` 变体）构建有向图，其余等级（如人行道）不参与路径计算
- 图只取最大弱连通分量，避免孤立/不连通的路段导致路径计算失败
- 坐标统一投影到 `EPSG:32650`（UTM Zone 50N）计算真实米制距离和吸附
- 严格最短路用 `networkx.bidirectional_dijkstra`；备选路径通过随机排除部分边重新求解，只接受长度 ≤ 严格最短路 `max_ratio`（默认 1.20，即120%）的结果，最多尝试 `max_attempts`（默认5）次
- 大批量 OD 用 `multiprocessing` 并行计算，支持 `--shard-count`/`--shard-index` 做分布式分片

## 文件说明

| 文件 | 作用 |
|---|---|
| `shortest_route_pipeline.py` | 核心库：读取OD Shapefile与道路Shapefile、构建有向图、吸附OD点到路网、计算严格最短路径 |
| `route_set_pipeline.py` | 在最短路基础上扩展生成"近似最优路径集"（严格最短路 + 1条≤120%的备选路），支持并行计算（`generate_path_sets_parallel`） |
| `run_path_sets.py` | **CLI 入口**，串联以上两个模块，见下方用法 |
| `merge_to_gpkg.py` | 把并行分片输出的多份路径 Shapefile，与原始 OD 行程（车辆ID、时间等）关联合并成一份 `optimal_path_sets.gpkg` |

## 依赖

```bash
pip install geopandas networkx shapely pandas tqdm
```

## 用法

```bash
# 全量计算，默认参数
python run_path_sets.py --trip-dir trip --road-file beijing/2017年北京市道路数据.shp

# 只跑前1000个OD做抽样测试，输出到自定义目录
python run_path_sets.py --limit 1000 --output-root output/test_run

# 分布式跑第0/2分片，8进程并行，跑完做逐个回读验收
python run_path_sets.py --shard-count 2 --shard-index 0 --workers 8 --validate

# 把各分片结果合并为一份GPKG（需要先编辑脚本内 SHARD_DIRS 常量指向实际输出目录）
python merge_to_gpkg.py
```

输入的 OD Shapefile 需要包含 `trip_id`/`taxi_id`/`start_lon`/`start_lat`/`end_lon`/`end_lat`/`is_plausib` 字段（对应 05 里 `03_extract_od_trips.py` 的输出格式）。输出 `path_set_index.csv` 汇总每个OD的计算状态和候选路径数，`optimal_path_sets.gpkg` 是最终合并的路径集合，字段含 `route_id`/`alt_rank`（1=严格最短路）/`dist_m`（路径长度）/`ratio`。
