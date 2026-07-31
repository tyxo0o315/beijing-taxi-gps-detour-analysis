# 数据预处理

把原始出租车轨迹的 Tab 分隔 TXT（13 列）转换成排序好的 9 列核心 CSV，是整条流水线的第一步。

## 处理逻辑

- 原始 13 列里，第 1 列更像公司/分组编号，抽样统计确认第 2 列才是车辆唯一 ID
- 经纬度原始存储为放大 100000 倍的整数，需要除以 `100000` 还原成 WGS84 经纬度
- 状态字段是中文文本，脚本从中提取两个布尔标记：文本包含"定位有效"→ `positioning_valid=1`，包含"重车"→ `occupied=1`
- 用外部归并排序（先按 `--chunk-rows`（默认 50 万行）分块在内存中按 `(taxi_id, timestamp)` 排序落盘，再用 `heapq.merge` 做多路归并）处理任意大小的文件，不要求整份数据能放进内存
- 格式不对（列数不是 13）或数值字段无法解析的行会被跳过并打印警告，不会中断整个转换

## 文件

| 文件 | 说明 |
|---|---|
| `convert_to_csv.py` | 主脚本，见下方用法 |

## 输出列

`group_id, taxi_id, timestamp, speed, latitude, longitude, direction, positioning_valid, occupied`

## 用法

```bash
python convert_to_csv.py 原始数据.txt 输出.csv
python convert_to_csv.py 原始数据.txt 输出.csv --limit 100000        # 只转前10万条，抽样测试
python convert_to_csv.py 原始数据.txt 输出.csv --chunk-rows 200000   # 调整内存排序块大小
```

`--output` 对应的文件不能已存在（脚本会拒绝覆盖）。此脚本无第三方依赖，纯标准库即可运行。
