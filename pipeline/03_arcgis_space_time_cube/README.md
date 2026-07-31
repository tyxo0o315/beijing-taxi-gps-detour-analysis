# ArcGIS 原生时空立方体（Space-Time Pattern Mining）

用 ArcGIS Pro 自带的 `arcpy.stpm`（Space-Time Pattern Mining 工具箱）把真实行程折线数据构建成
原生时空立方体（`.nc`格式），这是与 `03_密度与热点分析(opensource_pipeline)/11_build_space_time_cube.py`
（简单多波段GeoTIFF堆叠，纯numpy/rasterio实现）**不同的独立实现**——用的是ArcGIS官方的时空立方体
数据结构和算法，不是自己手写的近似版。

## 内容

`space_time_cube_arcgis.ipynb` 共7个代码格，在ArcGIS Pro Notebook环境里跑的：

1. `arcpy.stpm.MakeSpaceTimeCubeLayer`：把行程折线（`trip\real-trips-line\01-07.gpkg`）构建成
   500m格网/1小时步长的时空立方体
2. 检查/打印立方体信息（`DescribeSpaceTimeCube_stpm`）
3. 另一版参数（1000m格网/1天步长）的立方体构建
4. `VisualizeSpaceTime...`（3D可视化，最后一格未运行完）

## 依赖 / 运行环境

**必须在装了 ArcGIS Pro 的机器上、用 ArcGIS Pro Notebook 环境打开**（需要 Space-Time Pattern
Mining 扩展许可）。系统 Python 或 Colab 都无法运行这个笔记本，`import arcpy` 会直接失败。

## 注意事项

- 笔记本里的路径是原始项目里的绝对路径（`E:\summercamp\出租车数据\1\deliverables\trip\real-trips-line\...`），
  拿到别的机器/目录结构下需要按实际情况改
- 输入数据 `01-07.gpkg`（7天行程折线）本身没有一起拷贝进这个文件夹（体积较大），需要自己从原项目里找，
  或者用 `01_出租车拥堵OD提取流水线` 里 `03_extract_od_trips.py --trip-lines-output` 重新生成

## 原始来源

`E:\summercamp\1\MyProject\新建笔记本.ipynb`（ArcGIS Pro 工程 `MyProject.aprx` 自带的 Notebook）
