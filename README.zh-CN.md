<p align="right"><a href="README.md">English</a></p>

<h1 align="center">北京出租车 GPS 拥堵绕行分析</h1>

<p align="center">
<img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License">
<img src="https://img.shields.io/badge/python-3.9%2B-blue.svg" alt="Python">
<img src="https://img.shields.io/badge/domain-GIS%20%2F%20空间数据分析-green.svg" alt="GIS">
</p>

一条 8 阶段的空间数据处理流水线，把北京出租车原始 GPS 轨迹逐步转化为**"拥堵引发绕行"的判定信号**：从千万级原始 GPS 定位点出发，提取持久性交通热点、识别拥堵路段、重建乘客上下车（OD）行程、匹配到实际路网、计算理论最优路径，最终标记出那些实际行驶距离显著超过理论最短路径的"绕行"行程。

这是一个**团队项目**，本仓库整理了该流水线各阶段的代码与相关参考资料。

## 流水线总览

```mermaid
flowchart LR
    A["01 数据预处理<br/>原始TSV → 排序核心CSV"] --> B["02 密度与热点分析<br/>KDE密度栅格、六边形网格、<br/>持久性热点挖掘"]
    B --> C["04 拥堵识别<br/>剔除长时间停车段、<br/>标注低速拥堵段"]
    C --> D["05 出租车拥堵OD提取<br/>上下客三态状态机"]
    D --> E["06 道路等级匹配<br/>匹配最近道路、<br/>标注早晚高峰"]
    E --> F["07 最优路径<br/>最短路径 + 备选路径集"]
    F --> G["08 轨迹线∩最优路径<br/>绕行比判定"]
    B -. 可选，需<br/>ArcGIS Pro + arcpy .-> H["03 ArcGIS原生<br/>时空立方体"]

    classDef stage fill:#eef4ff,stroke:#4a6cf7,color:#1a1a2e;
    classDef optional fill:#fff3e0,stroke:#f5a623,color:#1a1a2e,stroke-dasharray: 4 3;
    class A,B,C,D,E,F,G stage;
    class H optional;
```

根目录的两个 notebook —— [`taxi_gps_pipeline_zh.ipynb`](taxi_gps_pipeline_zh.ipynb)（中文）和 [`taxi_gps_pipeline_en.ipynb`](taxi_gps_pipeline_en.ipynb)（英文）—— 是同一条 8 阶段流水线的单文件中英双语镜像（各 66 个 cell）。默认安全干跑模式（`EXECUTE_PIPELINE = False`），且会优雅跳过缺失的可选依赖，适合在深入各子模块脚本之前先通读一遍建立整体认识。

## 亮点算法：拥堵触发的起讫点(OD)提取

阶段4的核心思路在 [`pipeline/05_taxi_congestion_od_extraction/03_extract_od_trips.py`](pipeline/05_taxi_congestion_od_extraction/03_extract_od_trips.py)（`find_trips_for_vehicle` 函数）：一个轻量的三态状态机，把逐车辆的GPS点流转化为起讫点(OD)行程 —— 但只有"上客动作确实是被拥堵触发的"才计入一趟行程：

- **起点(O)**：车辆从"空车"变为"载客"的那个点 —— 但**仅当**这次跳变前一个点被标记为 `congestion == 1` 时才成立。如果上客前没有处于拥堵状态，这次上客会被直接丢弃，不计入行程。
- **终点(D)**：这段载客状态结束、变回空车后的第一个点 —— 这一步不要求拥堵。

```mermaid
stateDiagram-v2
    [*] --> EmptyFree
    EmptyFree: 空车·畅通
    EmptyJam: 空车·拥堵
    Occupied: 载客

    EmptyFree --> EmptyJam: 拥堵标记 → 1
    EmptyJam --> EmptyFree: 拥堵标记 → 0
    EmptyJam --> Occupied: 上客(0→1) — 记为起点★
    EmptyFree --> Occupied: 上客(0→1) — 丢弃，未满足拥堵前提
    Occupied --> EmptyFree: 下客(1→0) — 记为终点★
    Occupied --> EmptyJam: 下客(1→0) — 记为终点★
```

**为什么这个门槛有分析价值**：这个前提条件精确筛出了"乘客恰好是在出租车被堵住时上车"的那部分行程 —— 也正是后续 `07_optimal_routing`/`08_trajectory_optimal_path_intersection` 要检验"是否绕行"的目标群体，而不是把绕行信号稀释到所有行程里，无论它们最初是怎么开始的。

**实现细节**：
- 状态跳变边界用一次向量化的 `numpy.where(occ[1:] != occ[:-1])` diff 找出（每辆车只扫一次），而不是逐行Python循环判断，千万级GPS点也能跑得动。
- 每趟行程还会标记 `is_plausible`：同时用haversine直线距离**和**时长上限来判断，而不是只看速度——曾经真实遇到过一次时间戳被错记成未来6年的bug，算出来的隐含平均速度只有约200米/小时（远低于150km/h的上限，只看速度的话会被判为"合理"），但这趟行程实际耗时其实是1.89亿秒（约6年）。正是这个单独的时长上限才兜住了这一类错误。

## 仓库结构

```
.
├── taxi_gps_pipeline_zh.ipynb         # 主流水线 notebook（中文）
├── taxi_gps_pipeline_en.ipynb         # 主流水线 notebook（英文）
└── pipeline/
    ├── 01_data_preprocessing/                       # 阶段1 — 原始TSV转排序CSV
    ├── 02_density_hotspot_analysis_opensource/       # 阶段2 — KDE密度、六边形网格、持久性热点，纯开源(不依赖arcpy)
    ├── 03_arcgis_space_time_cube/                    # 可选 — arcpy/ArcGIS Pro时空立方体
    ├── 04_congestion_detection/                      # 阶段3 — 拥堵路段识别
    ├── 05_taxi_congestion_od_extraction/             # 阶段4 — 上下客(OD)行程提取
    ├── 06_road_grade_matching/                       # 阶段5 — GPS点匹配最近道路 + 时段标注
    ├── 07_optimal_routing/                           # 阶段6 — 最短路径 + 近似最优路径集
    └── 08_trajectory_optimal_path_intersection/      # 阶段7 — 真实轨迹与最优路径比较 → 绕行标签
```

每个子文件夹都有自己的 `README.md`，包含该阶段具体的 CLI 用法、输入/输出字段说明和方法说明。

## 依赖环境

流水线绝大部分是纯开源 Python（`geopandas`、`shapely`、`rasterio`、`networkx`、`pandas`、`numpy`、`pyproj`、`pyogrio`、`tqdm`），可以在任意标准环境或云端/CI runner 上复现。只有两处需要有授权的 **ArcGIS Pro** + `arcpy`：

- `pipeline/03_arcgis_space_time_cube/` — 整个文件夹（原生 `arcpy.stpm` 时空立方体）
- `pipeline/05_taxi_congestion_od_extraction/build_congestion_points.py` — 点转矢量的 arcpy 版本；同目录下功能完全等价的开源版 `01_build_points.py` **不需要** arcpy

安装核心开源依赖：

```bash
pip install numpy pandas pyproj rasterio geopandas shapely networkx pyogrio tqdm
```

（`pipeline/02_density_hotspot_analysis_opensource/requirements.txt` 列出了该阶段专用的最小依赖集。）

## 数据说明

本仓库**不包含**任何真实/原始的出租车行程 GPS 数据 —— 这类数据涉及隐私，有意排除在外。仓库中只包含流水线运行所需的公开参考地理数据：北京市省/县级行政区划边界（`02_density_hotspot_analysis_opensource/boundary/`）和 2017 年北京市路网 shapefile（`06_road_grade_matching/2017_Beijing_road.*`），以及一份可用合成数据完整跑通全流程的演示 notebook（`05_taxi_congestion_od_extraction/notebook/taxi_od_pipeline_part1-5.ipynb`），无需真实数据即可运行体验。

## License

本项目基于 [MIT License](LICENSE) 开源。
