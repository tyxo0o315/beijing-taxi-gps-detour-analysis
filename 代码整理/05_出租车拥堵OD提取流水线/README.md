# 出租车拥堵OD提取流水线

字段假设：车辆id、时间戳(unix秒)、经纬度、`congestion`(0/1是否拥堵)、`occupied`/`pickup`(是否载客)。

流程：**拥堵CSV转矢量点 → 按研究区面筛选合格车辆 → 起讫点三态状态机提取 → 起终点拆分成长表**。

## 文件说明

| 文件 | 作用 | 依赖 |
|---|---|---|
| `build_congestion_points.py` | 拥堵CSV转矢量点（**arcpy版**，X=经度Y=纬度普通二维点，`congestion`是普通属性字段不是Z值）。pandas分块读+`arcpy.da.NumPyArrayToFeatureClass`整块批量写，比逐行`InsertCursor`快一个数量级，内存占用恒定。 | 只能用ArcGIS Pro自带的 `python.exe`（一般在 `...\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe`）运行，系统Python装不了arcpy |
| `01_build_points.py` | 同上功能的**开源版**（不依赖arcpy），pandas+geopandas+pyogrio | `pip install pandas numpy geopandas pyogrio shapely` |
| `02_filter_by_study_area.py` | 按研究区面筛选车辆：同一辆车当天只要有任意一个载客点落在研究区内，就保留该车**当天全部**GPS点（不是只留区内那一段）——因为起讫点状态机需要完整前后文才能正确判断起终点。用`shapely.contains_xy`向量化做点在面内判断。 | 同上 |
| `03_extract_od_trips.py` | **核心算法**：起讫点三态状态机。起点＝空车状态下拥堵(`congestion==1`)，紧接着变成载客（`0→1`跳变）；终点＝载客状态紧接着变回空车（`1→0`跳变，不要求终点拥堵）。一辆车可有多段行程；起点条件不满足的跳变不会产生虚假终点；数据末尾未等到终点的行程单独记入`incomplete_trips.csv`。同时用haversine距离+时长做`is_plausible`合理性标记（隐含速度超150km/h **或** 单趟耗时超1天都判不合理——只看速度会漏判"时间戳错记导致耗时极长但算出来的速度反而正常"这种情况）。可选`--trip-points-output`/`--trip-lines-output`额外导出每趟行程的中间GPS轨迹点/连好的LineString。 | 同上 |
| `run_all.py` | 把上面三步串成一条命令，支持`--days`多天批跑、`--skip-build-points`跳过转点步骤。 | 同上 |
| `build_od_points.py` | 把`trips.csv`这种宽表（`start_*`/`end_*`两组列）拆成起点+终点在同一份文件里的长表，用`point_type`（`start`/`end`）字段区分，`trip_id`关联同一趟行程。 | 同上 |
| `study_area/insect-area.shp` | 参考数据：项目里实际用的研究区面（`02_filter_by_study_area.py --study-area-shp`的输入），WGS84坐标系 | — |
| `notebook/taxi_od_pipeline_part1-5.ipynb` | 用合成数据演示整条流水线的Jupyter/Colab笔记本，已用`jupyter nbconvert --execute`验证跑通，14个代码格全部无报错。可直接打开跑一遍确认逻辑，或改成读自己的真实CSV。 | 见notebook首格`%pip install` |

## 示例运行命令

```bash
# 开源版全流程（7天，列名用默认值：taxi_id/timestamp/latitude/longitude/congestion/occupied）
python run_all.py --base-dir <7天CSV所在目录> --study-area-shp study_area/insect-area.shp

# 只跑起讫点提取那一步（假设已有筛选后的逐点CSV）
python 03_extract_od_trips.py --input filtered.csv --output trips.csv \
    --id-col taxi_id --time-col timestamp --lon-col longitude --lat-col latitude \
    --congestion-col congestion --pickup-col occupied --occupied-value 1

# arcpy版转点（需要ArcGIS Pro自带python.exe）
"...\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" build_congestion_points.py --base-dir <你的CSV目录>
```

所有列名都可以用对应的 `--xxx-col` 参数覆盖，不用改代码。

## 原始来源（供溯源）

- `E:\summercamp\出租车数据\是否堵车\build_congestion_points.py`
- `E:\summercamp\出租车数据\od_pipeline_package\{01_build_points,02_filter_by_study_area,03_extract_od_trips,run_all}.py`
- `E:\summercamp\tools\build_od_points.py`
- `E:\summercamp\出租车数据\study_area\newest-area\insect-area.*`
- `E:\summercamp\出租车数据\notebooks\taxi_od_pipeline_part1-5.ipynb`

（`od_congestion_extraction\extract_od_trips.py` 与这里的 `03_extract_od_trips.py` 逐字节相同，是重复文件，没有单独收录。）
