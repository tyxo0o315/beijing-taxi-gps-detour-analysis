# OD轨迹线集与最优路径集求交算法

流水线的最后一步：把真实的出租车行程轨迹线，和 `07_最优路径` 算出的理论最短路径做比较，判断这趟行程是否"绕路"。

流程：**筛出早高峰且经过研究区域的最优路径 → 与真实轨迹按行程ID关联 → 计算实际长度/最短路长度之比 → 输出绕行标签**

## 文件说明

| 文件 | 作用 | 输入 | 输出 |
|---|---|---|---|
| `路径匹配.py` | **步骤1**：从 `07` 产出的 `optimal_path_sets.gpkg` 中筛出北京时间早高峰（7:00-9:00，兼容 Unix 时间戳/字符串/datetime64 三种时间格式）的路径；**步骤2**：与研究区域面（`valid_area_wgs84.gpkg`，会自动重投影对齐坐标系）做空间求交（`sjoin` + `intersects`），标记路径是否经过研究区域（`in_study_area` 0/1） | `optimal_path_sets.gpkg`、`valid_area_wgs84.gpkg` | `filtered_paths.gpkg` / `filtered_paths.csv` |
| `路径匹配3.py` | 用 `route_id`（`日期_trip_id`）把上一步的严格最短路长度（`alt_rank==1`）和每日真实轨迹线（`2017030{d}_trip_lines_filtered.gpkg`）关联，在 `EPSG:32650` 下计算真实轨迹的实际长度，与最短路长度做比值，得到绕行标签 `detour`（未匹配到最短路的记为 `-1`） | `filtered_paths.gpkg`、`2017030{d}_trip_lines_filtered.gpkg`（d=1~7，来自05的`--trip-lines-output`） | `2017030{d}_trip_lines_labeled.gpkg` |

> **注意**：`路径匹配3.py` 文件头部注释写的判定规则是"比值 ≥ 1.5 记为绕行"，但当前代码实际实现的阈值是 `ratio >= 1.0`（即只要实际长度不短于理论最短路就算 1）。使用前建议先确认这处差异是否符合你的分析需求，需要 1.5× 阈值的话把 `路径匹配3.py` 第64行的 `1.` 改成 `1.5` 即可。

## 依赖

```bash
pip install geopandas pandas shapely
```

## 用法

```bash
python 路径匹配.py    # 生成 filtered_paths.gpkg / filtered_paths.csv
python 路径匹配3.py   # 依次处理第1~7天，生成每天的 *_trip_lines_labeled.gpkg
```

两个脚本都是**改脚本顶部配置常量、直接运行**的写法（不是 argparse CLI），文件路径按需修改脚本内的 `PATH_FILE`/`AREA_FILE`/`RAW_PATTERN` 等常量。
