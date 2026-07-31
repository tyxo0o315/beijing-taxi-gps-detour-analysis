# 拥堵识别

两个独立脚本，串联起来完成"剔除长时间停车 → 标注拥堵段"，是 `05_出租车拥堵OD提取流水线` 所需拥堵标签的早期原型实现（后续在 `02_密度与热点分析(opensource_pipeline)/17_congestion_analysis.py` 中有更完整的版本）。

流程：**CSV → SQLite 排序 → 剔除长时间零速停车段 → 标注低速拥堵段 → CSV**

## 文件说明

| 文件 | 作用 |
|---|---|
| `数据清洗1.py` | 把 CSV 导入临时 SQLite 表并按 `(taxi_id, timestamp)` 建索引排序，然后流式扫描：连续零速（`speed_gps_kmh == 0`）超过 `MAX_ZERO_DURATION_MIN`（默认 60 分钟）的整段视为停车，整体丢弃；其余数据原样保留输出 |
| `拥堵判断.py` | 在清洗后的 CSV 上按车辆分组流式扫描，连续低速（`speed_gps_kmh < SPEED_THRESHOLD_KMH`，默认 20km/h）持续 ≥ `MIN_DURATION_SEC`（默认 240 秒/4分钟）的整段标记为 `congestion=1`，其余为 `0`；用临时文件 + `os.replace` 原子替换原文件 |

## 依赖

```bash
pip install pandas  # 仅用到标准库 csv/sqlite3/os/math，pandas 非必需
```

两个脚本都只依赖 Python 标准库（`csv`、`sqlite3`、`os`、`math`），无需额外安装第三方包。

## 用法

两个脚本都是**改配置区常量、直接运行**的写法（不是 argparse CLI），使用前需要打开脚本，修改顶部"配置区"里的输入/输出文件名、列名（`COL_TAXI_ID`/`COL_TIMESTAMP`/`COL_SPEED`）和阈值参数，再执行：

```bash
python 数据清洗1.py      # 生成 OUTPUT_CSV（已剔除长时间停车段）
python 拥堵判断.py       # 读取上一步的输出，原地追加 congestion 列
```

CSV 必须包含车辆 ID、Unix 秒时间戳、速度（km/h）三列，列名需与脚本顶部配置一致。
