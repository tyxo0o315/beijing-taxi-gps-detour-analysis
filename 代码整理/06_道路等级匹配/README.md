# 道路等级匹配

把GPS点(csv)匹配到最近的路网线要素，取道路等级`type`属性，输出成带`road_type`字段的点shapefile；
顺带按时间戳打早高峰(7-9点)/晚高峰(17-19点)/平峰标签。

## 方法

最初讨论过"给路网按等级建缓冲区+按优先级擦除重叠部分+点在多边形内判断"，可行但实现复杂、
计算量大（千万级点+5万+条路网线的缓冲区擦除）。改用 **最近邻查询+最大距离阈值**：
用 shapely 2.0 的 `STRtree`（R树空间索引，C实现）给每个点找最近的路网线段，距离超过
`--max-distance`（默认30米）就不赋值——效果上跟缓冲区要解决的问题（路网线没有宽度，需要一个
容差范围）等价，但不需要构造/裁剪缓冲区多边形，更快更简单。

坐标先投影到 UTM Zone 50N（EPSG:32650，北京所在分带）算真实米制距离，跟输入/输出用的
WGS84经纬度坐标系不是一回事，仅在内部距离计算时使用。

## 文件

| 文件 | 说明 |
|---|---|
| `match_road_type.py` | 主脚本，见下方用法 |
| `2017_Beijing_road.*` | 参考数据：北京路网shapefile，`type`字段是4类简化道路等级（城市支路/城市次干路/高架及快速路/城市主干路）。**必须和脚本放在同一目录**——脚本默认用 `os.path.join(SCRIPT_DIR, "2017_Beijing_road.shp")` 找它，挪到子文件夹会找不到（除非用`--road-shp`手动指定新路径） |
| `README_original.md` | 项目里原有的说明文档，原样保留 |

## 依赖

```bash
pip install pandas numpy geopandas shapely pyproj
```

## 用法

```bash
python match_road_type.py --input 我的GPS点.csv
python match_road_type.py --input 我的GPS点.csv --output 结果.shp --max-distance 50
python match_road_type.py --input 我的GPS点.csv --lat-col lat --lon-col lon --ts-col ts
python match_road_type.py --input 我的GPS点.csv --period 早高峰   # 只要早高峰(7-9点)的点
```

GPS点csv必须包含经纬度两列（默认列名`latitude`/`longitude`）和时间戳列（默认`timestamp`，unix秒）。
千万级数据会自动分块处理，输出shapefile超过200万行/2GB上限会自动分成`_part2`/`_part3`等文件。

## 原始来源

`E:\summercamp\出租车数据\road_type_matching\match_road_type.py`（及同目录的 `2017_Beijing_road.*`、
`README.md`，打包版 `road_type_matching.zip` 内容相同）。
