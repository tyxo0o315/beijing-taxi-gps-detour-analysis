<p align="right"><a href="README.md">English</a></p>

# 北京出租车 GPS 拥堵绕行分析

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)
![GIS](https://img.shields.io/badge/domain-GIS%20%2F%20空间数据分析-green.svg)

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

根目录的两个 notebook —— [`出租车GPS全流程.ipynb`](出租车GPS全流程.ipynb)（中文）和 [`出租车GPS全流程_English.ipynb`](出租车GPS全流程_English.ipynb)（英文）—— 是同一条 8 阶段流水线的单文件中英双语镜像（各 66 个 cell）。默认安全干跑模式（`EXECUTE_PIPELINE = False`），且会优雅跳过缺失的可选依赖，适合在深入各子模块脚本之前先通读一遍建立整体认识。

## 仓库结构

```
.
├── 出租车GPS全流程.ipynb              # 主流水线 notebook（中文）
├── 出租车GPS全流程_English.ipynb      # 主流水线 notebook（英文）
└── 代码整理/
    ├── 01_数据预处理/                              # 阶段1 — 原始TSV转排序CSV
    ├── 02_密度与热点分析(opensource_pipeline)/       # 阶段2 — KDE密度、六边形网格、持久性热点，纯开源(不依赖arcpy)
    ├── 03_ArcGIS原生时空立方体/                     # 可选 — arcpy/ArcGIS Pro时空立方体
    ├── 04_拥堵识别/                                 # 阶段3 — 拥堵路段识别
    ├── 05_出租车拥堵OD提取流水线/                    # 阶段4 — 上下客(OD)行程提取
    ├── 06_道路等级匹配/                             # 阶段5 — GPS点匹配最近道路 + 时段标注
    ├── 07_最优路径/                                 # 阶段6 — 最短路径 + 近似最优路径集
    └── 08_OD轨迹线集与最优路径集求交算法/            # 阶段7 — 真实轨迹与最优路径比较 → 绕行标签
```

每个子文件夹都有自己的 `README.md`，包含该阶段具体的 CLI 用法、输入/输出字段说明和方法说明。

## 依赖环境

流水线绝大部分是纯开源 Python（`geopandas`、`shapely`、`rasterio`、`networkx`、`pandas`、`numpy`、`pyproj`、`pyogrio`、`tqdm`），可以在任意标准环境或云端/CI runner 上复现。只有两处需要有授权的 **ArcGIS Pro** + `arcpy`：

- `代码整理/03_ArcGIS原生时空立方体/` — 整个文件夹（原生 `arcpy.stpm` 时空立方体）
- `代码整理/05_出租车拥堵OD提取流水线/build_congestion_points.py` — 点转矢量的 arcpy 版本；同目录下功能完全等价的开源版 `01_build_points.py` **不需要** arcpy

安装核心开源依赖：

```bash
pip install numpy pandas pyproj rasterio geopandas shapely networkx pyogrio tqdm
```

（`代码整理/02_密度与热点分析(opensource_pipeline)/requirements.txt` 列出了该阶段专用的最小依赖集。）

## 数据说明

本仓库**不包含**任何真实/原始的出租车行程 GPS 数据 —— 这类数据涉及隐私，有意排除在外。仓库中只包含流水线运行所需的公开参考地理数据：北京市省/县级行政区划边界（`02_.../boundary/`）和 2017 年北京市路网 shapefile（`06_道路等级匹配/2017_Beijing_road.*`），以及一份可用合成数据完整跑通全流程的演示 notebook（`05_.../notebook/taxi_od_pipeline_part1-5.ipynb`），无需真实数据即可运行体验。

## License

本项目基于 [MIT License](LICENSE) 开源。
