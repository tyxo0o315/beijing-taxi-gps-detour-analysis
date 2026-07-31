# GPS点匹配道路类型 —— 独立小工具

把GPS点(csv)匹配到最近的道路，取道路等级(`type`字段)写成新属性，输出成带
`road_type`字段的点shapefile。跟出租车数据那套大pipeline是独立的，不依赖它的任何
文件，可以单独拿去用。

## 这里有什么
- `match_road_type.py` —— 主脚本
- `2017_Beijing_road.shp`(+`.shx`/`.dbf`/`.prj`/`.cpg`) —— 北京市道路网络，
  38,309条线段，`type`字段是4类道路等级：**城市支路 / 城市次干路 / 高架及快速路 /
  城市主干路**

## 环境要求
```bash
pip install pandas geopandas shapely pyproj
```

## 使用前必须做的事
**打开`match_road_type.py`，把`INPUT_CSV`这个变量改成你自己的GPS点csv文件路径**
(或者不改代码，直接用`--input`参数传路径)。这份代码本身不带GPS点数据，只带了
道路网络数据。

你的csv必须有经纬度两列，默认列名是`latitude`/`longitude`，如果你的列名不一样
(比如`lat`/`lon`)，用`--lat-col`/`--lon-col`指定，不用改代码。

## 怎么跑
```bash
python match_road_type.py --input 你的GPS点.csv
python match_road_type.py --input 你的GPS点.csv --output 结果.shp --max-distance 50
python match_road_type.py --input 你的GPS点.csv --lat-col lat --lon-col lon --ts-col ts
python match_road_type.py --input 你的GPS点.csv --period 早高峰   # 只要早高峰(7-9点)的点
```

## 时段标签(早高峰/晚高峰/平峰)
按csv里的时间戳列(unix秒，默认列名`timestamp`，不一致用`--ts-col`指定)算北京时间
的小时，每个点打上`time_period`标签：**早高峰(7~9点)/晚高峰(17~19点)/平峰(其它
时间)**。不传`--period`参数就是全部时段都保留、只是每行多一个标签列；传了
`--period 早高峰`/`晚高峰`/`平峰`就只输出对应时段的点。

`--max-distance`(默认30米)：点到最近道路的距离超过这个值就不赋`road_type`(留空)，
不强行匹配到太远的路。阈值放宽能覆盖更多点，但在路口密集/道路平行靠近的地方，
阈值越大匹配到"错误的相邻道路"的风险也越高，需要按你的数据实际情况调整，没有
放之四海而皆准的默认值。

## 方法说明
道路网络本身是没有宽度的线，最初讨论过"给道路按等级建缓冲区、按优先级擦除重叠
部分、点在多边形内判断"这个思路，可行但实现复杂、计算量大。这里改用**最近邻查询
+最大距离阈值**：用shapely 2.0的`STRtree`(R树空间索引)给每个点找最近的道路线段，
超过距离阈值就不赋值——效果上是等价的(路没有宽度，需要一个容差范围)，但不需要
构造/裁剪缓冲区多边形，也不需要给每个道路等级单独定缓冲区宽度和优先级，更快更
简单，千万级点也能跑得动。

## 输出
跟输入csv同样的行(或者按`--period`筛选后的子集)、同样的列，加两个字段——
`road_type`(未匹配到道路的留空)和`time_period`(早高峰/晚高峰/平峰)，存成点
shapefile，坐标系跟原始GPS点一致(WGS84经纬度)。可以直接用QGIS/ArcGIS Pro打开。

**已知限制**：
- ESRI Shapefile的DBF字段名最长10个字符，原始csv里超过10字符的列名会被自动截断
  (比如`positioning_valid`会变成`positionin`)，这是GDAL的正常行为，不是bug，脚本
  跑的时候会打印警告。如果你的列名截断后会互相重名(比如两个字段截断后变成一样的
  名字)，需要自己先在csv里改列名再跑。
- Shapefile单文件有2GB上限，如果GPS点数量特别大(千万级)，脚本会自动按
  `--max-rows-per-shp`(默认200万行)分成多个文件(`_part2.shp`、`_part3.shp`...)，
  不会因为超限而写坏或报错。如果不想要这个限制，把脚本最后`to_file`那行的
  `driver="ESRI Shapefile"`换成`driver="GPKG"`即可，GeoPackage没有这些限制。

## 实测参考
用出租车数据里10万行GPS点样本测试过：`--max-distance 30`(默认)匹配率约53%——
这份道路数据只有4类主干道路(不含小巷/支路以下的详细路网)，所以匹配率会比用更
详细的路网数据(比如包含service/footway等小路的完整OSM提取)低一些，属于正常现象，
不是bug；实际匹配率取决于你的道路数据详细程度和GPS点分布，建议先用小样本(比如
`--limit 10000`)跑一遍看看匹配率再决定要不要调`--max-distance`。
