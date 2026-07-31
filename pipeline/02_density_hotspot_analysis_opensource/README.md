# 出租车GPS数据分析 pipeline —— 开源等价方案

这是 `E:\summercamp\出租车数据\` 那套arcpy pipeline的开源版本，不依赖ArcGIS Pro/Server
授权，只用numpy/pandas/pyproj/rasterio/geopandas/shapely。目标场景：部署到没有ArcGIS
许可的云端服务器上跑。

## 阶段总览

| 阶段 | 脚本 | 说明 |
|---|---|---|
| 0 | `00_convert_to_csv.py` | 原始13列TSV -> core.csv(核心字段+已按车辆id/时间全局排序)，不依赖arcpy |
| 1 | `01_feature_engineering.py` | core.csv -> 带派生特征的features.csv(速度/航向/漂移标记/OD事件...) |
| 3~9 | `03~09_*.py` | 核密度栅格(KDE) + 六边形网格聚合，见下表 |
| 10 | `10_preview_png.py` | 把栅格/六边形结果渲染成PNG，不用装QGIS/ArcGIS就能看 |
| 11 | `11_build_space_time_cube.py` | 多个年份/日期的同主题栅格堆叠成时空立方体(见下文) |
| 12 | `12_spacetime_clustering.py` | 多天(比如一周7天)六边形聚合结果做时空聚类，识别持续/新增/减弱的热点聚集区(见下文) |
| 13 | `13_od_trip_extraction.py` | features.csv -> trips.csv(配对成完整行程) + idle_segments.csv(空驶找客段)，OD分析的基础输入(见下文) |
| 17 | `17_congestion_analysis.py` | 拥堵事件识别(速度<32km/h持续>3分钟) + 4级拥堵等级分类，按格子+小时聚合(见下文) |

(阶段2 = arcpy版的"建点入库"，这里不需要，开源版直接用pandas分块读CSV。14~16预留给
OD矩阵/流向图/距离时长分布/车辆利用率这几个后续分析，还没写，见"OD分析"一节。
GPS点匹配道路类型是独立小工具，不在这套编号pipeline里，见
`E:\summercamp\出租车数据\road_type_matching\`)

| 密度主题 | 脚本 | 输出 |
|---|---|---|
| 全量点密度(基线) | `03_density_baseline.py` | `kd_baseline_all.tif` |
| 重车/空车对比 | `04_density_occupied_vs_empty.py` | `kd_occupied.tif` / `kd_empty.tif` / `kd_occupied_minus_empty.tif` |
| 分时段(早晚高峰/平峰/夜间) | `05_density_by_timewindow.py` | `kd_time_*.tif` |
| 低速/拥堵候选点 | `06_density_low_speed.py` | `kd_low_speed.tif` |
| 疑似转向事件 | `07_density_turn_events.py` | `kd_turn_events.tif` |
| 上下客(OD)事件 | `08_density_od_events.py` | `kd_pickup.tif` / `kd_dropoff.tif` |
| 六边形网格聚合 | `09_grid_aggregation.py` | `hex_stats_all.gpkg`(点数/平均速度/平均航向变化/上下车计数) |

`09`比arcpy版多做了一件事：arcpy版09脚本在49.6M全量点上尝试过"每个六边形的点数/平均
GPS速度/平均航向变化"这个更完整的聚合，但因为分批统计的NULL偏差问题放弃了。这里的
流式sum/count累加法没有这个偏差，直接算出了完整结果。

## 核心方法说明

- **核密度(KDE)**：ArcGIS的`KernelDensity`默认用quartic(双权重)核函数，内部也是"点落
  网格再跟核函数卷积"。`kde_utils.py`直接复刻同一个核函数公式、同一套cell_size(默认
  150米)/search_radius(默认500米)语义，用FFT做卷积(numpy自带，不需要scipy——这台
  机器上`pip install scipy`遇到过哈希校验失败，为避免同样的不确定性，开源版全程不
  依赖scipy/h3)。不是逐像元数值对齐ArcGIS的输出，但核函数/带宽/分辨率完全一致。
- **六边形网格**：`hex_utils.py`里标准的axial坐标算法直接向量化把点分配到六边形，
  不逐点做空间连接，千万级点也很快。`--hex-size`参数跟arcpy版语义一致(面积=
  hex_size^2平方米，不是边长)。
- **北京市边界筛点**：用`boundary/province.shp`(2024年全国省市县三级行政区划数据，审图号
  GS(2024)0650号，从`E:\2024年省市县三级行政区划数据...\省.shp`拷贝过来，仓库里已重命名为英文文件名)提取北京市
  的面，`boundary_utils.py`用Shapely 2.0+的`contains_xy`做向量化点在多边形判断
  (内部有C实现的prepared geometry，千万级点可以跑得动)。这取代了最早版本"用一个
  经纬度矩形框圈研究区"的粗糙做法——矩形框会把跑到河北/天津境内但仍在框内的点也算
  进来，也会漏掉边界犬牙交错处框外但确实属于北京的点。**如果要分析其它城市，把
  `boundary_utils.py`里筛选条件的"110000"/"北京市"换成对应省代码/名称即可**，province.shp
  本身是全国数据，不需要换文件。
- **区县级边界**：`boundary/county.shp`(同一份行政区划数据的区县级)，`assign_beijing_district()`
  把点分配到北京市16个区之一(16个区各自一次向量化`contains_xy`查询，不是逐点循环)，
  给OD矩阵这类需要更粗空间聚合单元的分析用，见"OD分析"一节。
- **内存安全**：所有阶段都用`pandas.read_csv(..., chunksize=2_000_000)`分块处理，
  核密度的2D直方图和六边形的统计量都是可累加的，不需要把4900万行、17列的特征文件
  一次性读进内存。

## 可视化(10_preview_png.py)

跑完03~09之后：

```bash
python 10_preview_png.py
```

对`output/`下每个`kd_*.tif`和`hex_stats_all.gpkg`各字段生成同名PNG。为了不让结果图
"一片死黑"：
- 密度=0(没有数据)的像元统一显示成浅灰色背景，不落进色阶
- 默认用log1p做色阶拉伸(密度分布高度右偏，线性色阶下热点会把其它区域全部压成一个颜色)
- 默认用`turbo`(蓝->青->黄->红)配色，热点和背景的对比比黑底配色更清楚
- 默认过滤掉低于25百分位的孤立噪声像元(比如郊区/山区偶尔一个出租车GPS点)，只影响
  这张预览图的显示，不改GeoTIFF/GeoPackage里的原始数值。用`--min-percentile 0`关掉
  这个过滤，用`--linear`关掉log拉伸，用`--cmap`换配色方案。

## 在ArcGIS Pro里打开结果

这套开源pipeline的输出坐标系跟原来的arcpy pipeline完全一样(EPSG:32650 / UTM50N)，
如果手头正好有装了ArcGIS Pro的机器，可以直接把结果导入进去，跟arcpy版的.gdb图层
叠在一起对比：

**栅格(`kd_*.tif`)：**
1. 打开ArcGIS Pro，新建或打开一个工程
2. 在"目录"面板里找到`output/`文件夹，或者直接把`.tif`文件拖进地图视图/"添加数据"
3. 默认符号化通常是灰阶或者拉伸渲染；建议在图层的"符号系统"面板里改成"拉伸"
   (Stretch)渲染，色带选"Cividis"或者跟`10_preview_png.py`同款感觉的暖色系
   (比如"Temperature"、"Heat Map"色带)，并把拉伸类型改成"标准差"或者手动设一个
   Gamma做非线性拉伸(等效于我们脚本里的log1p)，不然大部分像元会因为密度分布右偏
   显得"一片同色"

**六边形(`hex_stats_all.gpkg`)：**
1. "添加数据" -> 浏览到`hex_stats_all.gpkg`，选里面的图层(GeoPackage可能包含多个图层，
   这里只有一个)
2. 在"符号系统"里选"分级色彩"，字段选`point_count`/`mean_speed_gps_kmh`/
   `pickup_count`等，分级方法建议"分位数"(Quantile)而不是"相等间隔"，原因同上
   (点数分布右偏)

**时空立方体(`11_build_space_time_cube.py`产出的多波段GeoTIFF)：**
1. 先用"添加数据"把这个多波段tif加进工程，确认能正常显示、波段数对(每个波段对应
   一个时间步/日期，波段描述里能看到标签)
2. 如果要用ArcGIS原生的时空分析工具(Emerging Hot Spot Analysis、Mann-Kendall趋势
   检验等)，用"Create Space Time Cube From Multidimensional Raster"这个地理处理工具，
   输入选这个多波段tif，它会转成ArcGIS原生的.nc时空立方体格式，之后就能接后续那些
   时空分析工具了

## 复用到其它年份/日期

`01_feature_engineering.py`和`05_density_by_timewindow.py`都支持`--date YYYY-MM-DD`
参数(默认`2017-03-01`)，用来判断"是否当天"和切分时段——**如果2017-03-01这一天跑
通、结果符合预期，换其它年份/日期的数据时不需要改代码**，流程是：

```bash
python 00_convert_to_csv.py raw/20180301.txt 20180301_core.csv
export TAXI_CORE_CSV=20180301_core.csv
export TAXI_FEATURES_CSV=20180301_core_features.csv
export TAXI_OUT_DIR=output_2018
python 01_feature_engineering.py --date 2018-03-01
python 03_density_baseline.py    # 04~09同理，--cell-size/--radius保持跟其它年份一致
...
python 05_density_by_timewindow.py --date 2018-03-01
```

每年用独立的`TAXI_OUT_DIR`，避免互相覆盖。

## 多年份的时空结合分析(空间+时间维度)

等多个年份都跑完了(每个年份一个`output_<年份>/`目录)，可以用`11_build_space_time_cube.py`
把同一个主题的栅格(比如每年的`kd_baseline_all.tif`)按年份堆叠成一个多波段GeoTIFF，
空间维度是像元、时间维度是波段，概念上对应ArcGIS Pro"Create Space Time Cube By
Aggregating Points"产出的时空立方体：

```bash
python 11_build_space_time_cube.py \
    --band 2017-03-01=output_2017/kd_baseline_all.tif \
    --band 2018-03-01=output_2018/kd_baseline_all.tif \
    --band 2019-03-01=output_2019/kd_baseline_all.tif \
    --out space_time_cube_baseline.tif
```

这里的时间维度目前是"一个日期一个波段"这种粗粒度，还不带Mann-Kendall趋势检验/
Emerging Hot Spot Analysis那样的统计判定——如果需要那些分析方法，按上一节"在ArcGIS
Pro里打开结果"里的步骤，把这个多波段tif导入ArcGIS Pro转成原生时空立方体格式即可；
或者用下一节的`12_spacetime_clustering.py`直接在开源环境里做同类分析，不需要ArcGIS。
所有参与堆叠的栅格必须用完全相同的`--cell-size`/`--radius`/研究区跑出来(脚本会自动
校验形状/坐标系/仿射变换是否一致，不一致会报错退出而不是静默错位)。

## 多天时空聚类(识别多个持续/新增/减弱的热点聚集区)

如果手上有连续多天的数据(比如一周7天：20170301~20170307)，每天各自跑一遍00~09后，
会得到7份`hex_stats_all.gpkg`(同样的`--hex-size`跑出来的六边形网格天然是对齐的，
不需要额外做空间配准——09脚本的六边形axial坐标是基于UTM绝对坐标算的，不依赖当天
数据的范围)。`12_spacetime_clustering.py`把这7天放在一起做时空聚类分析，概念上对应
ArcGIS Pro的"Emerging Hot Spot Analysis"(涌现热点分析)，纯numpy/pandas实现(不依赖
scipy/esda/pysal，这台机器上装scipy/h3时遇到过pip哈希校验失败，为避免同样的不确定性
继续不引入新依赖——Getis-Ord Gi*和Mann-Kendall都是标准公式，不难从头写)：

```bash
python 12_spacetime_clustering.py \
    --day 2017-03-01=output_20170301/hex_stats_all.gpkg \
    --day 2017-03-02=output_20170302/hex_stats_all.gpkg \
    --day 2017-03-03=output_20170303/hex_stats_all.gpkg \
    --day 2017-03-04=output_20170304/hex_stats_all.gpkg \
    --day 2017-03-05=output_20170305/hex_stats_all.gpkg \
    --day 2017-03-06=output_20170306/hex_stats_all.gpkg \
    --day 2017-03-07=output_20170307/hex_stats_all.gpkg \
    --value-field point_count \
    --out space_time_clusters.gpkg
```

方法：①每天单独算一次Getis-Ord Gi*局部统计量(每个六边形跟自身+6个邻居的和，
对比全局均值/标准差，得到"这个格子和周围是不是扎堆偏高"的z分数，不是看它自己绝对值
多高) ②每个格子把7天的Gi* z分数串成时间序列，做Mann-Kendall趋势检验(判断在增强/
减弱/无显著趋势) ③结合"显著热点天数"+趋势，把每个格子分类成`persistent_hotspot`
(持久热点)/`intensifying_hotspot`(增强热点)/`diminishing_hotspot`(减弱热点)/
`consecutive_hotspot`(连续新增热点)/`sporadic_hotspot`(零星热点)/
`oscillating_hotspot`(冷热振荡)/`no_pattern_detected`(无显著模式)这几类，简化自
ArcGIS的8分类体系 ④把"显著热点"的格子按六边形邻接关系做连通分量，**直接识别出
多个独立的聚集区**，不是一堆散落的格子——这是回答"识别多个聚集区"的核心步骤，输出
里的`cluster_id_last_day`字段就是每个聚集区的编号。

已经用合成的7天测试数据验证过整个流程能跑通、分类结果合理(测试用18,867个六边形，
几秒钟跑完，识别出67个独立聚集区)。**局限**：Getis-Ord Gi*和Mann-Kendall是标准公式，
但没有照搬ArcGIS Emerging Hot Spot Analysis的全部实现细节(比如多重检验的FDR校正)，
分类边界跟ArcGIS版可能对不上，适合看空间格局/相对排名，不要拿具体z值跟ArcGIS输出
做逐格数值对比；7天做趋势检验统计功效有限，天数越多结论越可信。

输出`space_time_clusters.gpkg`每个六边形一行，字段包括每天的原始值(`value_<日期>`)、
每天的Gi* z分数(`gi_star_<日期>`)、趋势检验结果(`mk_trend_z`)、模式分类(`category`)、
最后一天的聚集区编号(`cluster_id_last_day`，0=不属于任何聚集区)——在QGIS/ArcGIS Pro
里按`category`或`cluster_id_last_day`分类上色，就能直观看到哪些地方是持续热点、
哪些是新冒出来的、哪些正在退热。

## OD分析(起讫点、行程、供需匹配)

`01_feature_engineering.py`产出的`event_type`只是给每个GPS点打了`pickup`/`dropoff`标签，
是独立的点，不是"行程"。`13_od_trip_extraction.py`按`taxi_id`把连续的pickup->dropoff
事件配对起来(features.csv已经按taxi_id+时间排好序，单遍流式状态机处理，不需要额外
排序)，同时也顺带识别两趟行程之间的"空驶找客"阶段，产出两张表：

```bash
python 13_od_trip_extraction.py --hex-size 300
```

- `trips.csv`：每行一趟行程，起点/终点经纬度+时间、时长、沿途累计距离(`path_distance_m`)
  和直线距离(`straight_distance_m`)、平均速度、起终点所在六边形(`pickup_hex_q/r`、
  `dropoff_hex_q/r`)
- `idle_segments.csv`：每行一段"上一趟下车"到"下一趟上车"之间的空驶找客段，搜索时长/距离

**数据质量兜底**：阶段进行中如果遇到`trip_break=1`(间隔>1800s)或`bad_coord=1`，直接
丢弃这一整段正在累积的记录，不强行跨断点拼接。`trips.csv`还有一个`is_plausible`
标记列——`event_type`的宽松判定口径(见前面小节)偶尔会把非真实载客的状态跳变误判成
一趟"行程"，实测2M行样本数据里出现过时长16.7小时、平均速度506km/h这种明显异常的
记录。默认阈值是时长30秒~2小时、平均速度不超过120km/h，超出范围的`is_plausible=0`，
不删除(保留给专门研究异常记录的场景用)，下游分析建议先筛`is_plausible=1`再用。

基于`trips.csv`/`idle_segments.csv`可以做的分析（OD矩阵、流向图、距离时长分布、分
时段OD对比、车辆利用率、供需匹配效率，编号14~16预留），空间聚合单元建议用北京市
16个区(`boundary/county.shp`，用`boundary_utils.py`的`assign_beijing_district()`把
起讫点分配到区)而不是09/12默认的300米细六边形——细粒度网格做"格子对格子"的OD矩阵
会导致绝大多数格子对之间只有0-1趟行程，统计上没有意义，流向图也看不清；行政区是
更合适的聚合粒度。这几个分析脚本还没写，`trips.csv`/`idle_segments.csv`是它们共同
的输入基础。

## 拥堵分析(17_congestion_analysis.py)

用速度持续性规则识别拥堵事件，再按速度分4级：

```bash
python 17_congestion_analysis.py --speed-threshold 32 --min-duration-sec 180
```

**判定规则**(讨论后确定，不含"时间占有率"——那是基于固定断面检测器连续监测的概念，
出租车GPS是稀疏采样的浮动车数据，不适用这套定义)：
1. 沿单车连续轨迹(不跨`trip_break`/`bad_coord`)，`speed_gps_kmh<32km/h`连续持续
   超过3分钟(180秒)，整段标记为一次拥堵事件
2. 4级拥堵等级按速度分：畅通(>30km/h)/轻度拥挤(20~30)/拥挤(10~20)/严重拥挤(<10)，
   分别应用在每个拥堵事件自身的平均速度、以及每个"六边形+小时"的整体平均速度

输出两个文件：
- `congestion_events.csv`：拥堵事件明细，每行一次事件(起止时间/位置、时长、平均速度、
  等级、所在六边形)。**`likely_parked`标记列**——持续超过30分钟的"拥堵事件"大概率是
  车辆停驶/收车停在原地(速度一直趴在阈值下但不是真拥堵)，不是真实的交通拥堵，实测
  2M行样本里出现过持续24小时的这类记录。同样不删除，只标记，下游分析建议排除
  `likely_parked=1`的记录再统计真实拥堵情况
- `congestion_by_hex_hour.gpkg`：按六边形+小时(北京时间0~23)聚合，每行是"某格子某
  小时"的总点数、拥堵事件覆盖的点数、平均速度、4级等级——可以直接在QGIS/ArcGIS Pro
  里按`congestion_level`分类上色，或者按小时筛选看拥堵格局怎么随一天时间变化，也可以
  把`mean_speed_gps_kmh`列喂给`12_spacetime_clustering.py`的`--value-field`参数，
  在多天数据上识别持续性拥堵聚集区(注意Getis-Ord Gi*找的是"热点"，用在速度字段上
  要看**冷点**——低速扎堆的地方才是拥堵聚集区，不是速度高的热点)

## 路网类型匹配(18_road_type_matching.py)

把每个GPS点匹配到最近的路网线要素，取该路的等级(`fclass`，OSM标准分类：motorway/
trunk/primary/secondary/tertiary/residential/service/footway/path等)，作为新字段
写回GPS点数据——用于后续按道路等级分层分析(比如"主干道 vs 支路"的拥堵/速度对比)。

```bash
python 18_road_type_matching.py \
    --road-shp "E:\summercamp\2017年北京市道路数据.shp" \
    --input features.csv --output features_with_road_type.csv --max-distance 30
```

**方法**：不是"给路网建缓冲区+按等级优先级擦除重叠部分+点在多边形内判断"这个思路
(最初讨论过，可行但实现复杂、计算量大)，改用**最近邻查询+最大距离阈值**——用
shapely 2.0的`STRtree`(R树空间索引)给每个点找最近的路网线段，如果距离超过
`--max-distance`(默认30米)就不赋值，效果上等价于"缓冲区"要解决的问题(路网线没有
宽度，需要一个容差范围)，但不需要真的构造/裁剪缓冲区多边形，千万级点也能跑得动。

新增3列：`road_fclass`(道路等级英文代码)、`road_type_cn`(中文道路类型)、
`road_dist_m`(点到最近路的实际距离，米)。距离超过阈值的点这三列留空，不强行匹配。

**路网数据字段说明**：路网shapefile(`E:\summercamp\2017年北京市道路数据.shp`，OSM
提取，56,431条线段，26种`fclass`)的`.dbf`是GBK编码，`.cpg`声明可能对不上(踩过
跟`province.shp`/`county.shp`同样的坑)，脚本内部按几种编码依次尝试读取，不需要手动处理。

**阈值怎么选，实测参考**：用50万行样本测试过，`--max-distance 30`(默认)匹配率
65.5%，`--max-distance 100`匹配率84.7%——阈值放宽能覆盖更多点，但阈值越大，在
复杂路口/平行道路密集的地方，匹配到"错误的相邻道路"的风险也越高，是精度和覆盖率
的权衡，没有普适的"正确"阈值，建议按实际研究区域路网密度调整，匹配后检查
`road_dist_m`的分布(实测30米阈值下，匹配到的点里位数距离只有4.3米，说明真正匹配上
的点普遍很近，未匹配的那部分大概率是本身离最近路较远，不是阈值卡得太严导致的
误伤)。

路网数据本身体积较大(shp+dbf约55MB)，没有打包进`taxi_opensource_pipeline.zip`，
需要单独把`E:\summercamp\2017年北京市道路数据.shp`(连同同名的.shx/.dbf/.prj/.cpg)
传到云端服务器上，用`--road-shp`指定路径。

## 跟数据本身相关的已知局限(继承自上游convert_to_csv.py，这一层补不回来)

- `occupied=0`("空车密度")实际是"非重车"的合集(空车+驻车+停运+任务车+未知)，不是纯粹的"空车"
- `event_type`(pickup/dropoff)判定是"任意非重车状态->重车"的宽松判定，不是严格的"空车->重车"
- 没有`acc_on`字段，06脚本无法排除"熄火但仍报点"的静止记录
- `speed`列(设备自报速度)极少数是脏值(约300万分之一)，已在`01_feature_engineering.py`里
  处理成缺失值，不会导致整行报错

详见每个脚本文件顶部的docstring，写得比这里详细。

## 安装

```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 运行

把 `20170301_core.csv`(或已经跑好的 `20170301_core_features.csv`)放到跟脚本同一个目录，
或者用环境变量指定路径:

```bash
export TAXI_CORE_CSV=/path/to/20170301_core.csv
export TAXI_FEATURES_CSV=/path/to/20170301_core_features.csv
export TAXI_OUT_DIR=/path/to/output
export TAXI_BOUNDARY_SHP=/path/to/province.shp   # 默认用包内boundary/province.shp，一般不需要改
```

```bash
python 00_convert_to_csv.py raw_13col.txt 20170301_core.csv   # 只有原始TSV时才需要
python 01_feature_engineering.py                              # 产出features.csv
python 03_density_baseline.py
python 04_density_occupied_vs_empty.py
python 05_density_by_timewindow.py
python 06_density_low_speed.py
python 07_density_turn_events.py
python 08_density_od_events.py
python 09_grid_aggregation.py
python 10_preview_png.py                                       # 生成PNG预览
```

每个density脚本都支持 `--cell-size`/`--radius` 覆盖默认值(150米/500米)，具体参数看
`--help`或脚本docstring。

## 输出

`$TAXI_OUT_DIR/`(默认是脚本所在目录下的`output/`)下：
- `kd_*.tif` —— GeoTIFF栅格，坐标系EPSG:32650(UTM50N)，可以直接用QGIS/ArcGIS/rasterio打开
- `kd_*.png` —— 上面栅格的可视化预览(10_preview_png.py产出)
- `hex_stats_all.gpkg` —— GeoPackage矢量，每个六边形一行，字段
  `point_count/mean_speed_gps_kmh/mean_heading_delta_deg/pickup_count/dropoff_count`
- `hex_*.png` —— 上面各字段的可视化预览

## 实测耗时参考(仅供数量级参考)

用本机(Windows, 16GB内存)对完整特征文件的1,000,000行子集(约完整数据的2%)测试，
03~09每个脚本读完全部子集只需1-3秒(FFT卷积部分对~800x800的栅格几乎瞬间完成，
北京市边界的向量化点在多边形判断对百万级点也是秒级)。按行数线性外推，完整
49,911,405行数据全部脚本跑一遍，预计在**10-20分钟量级**(以CSV顺序读取吞吐为主要
瓶颈，实际取决于云端服务器的磁盘/CPU速度，仅供参考，不是精确承诺)。
