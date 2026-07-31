"""Terminal entry point for per-OD near-optimal path sets."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from route_set_pipeline import (
    PathSetConfig,
    generate_path_sets_parallel,
    validate_path_set_output,
)
from shortest_route_pipeline import (
    RouteConfig,
    build_graph,
    load_roads,
    load_trips,
    retain_largest_component,
    simplify_graph,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="为每个一一对应OD生成严格最短路和一条120%以内的随机替代路径"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="只处理前N个OD；省略则运行全量",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("output/path_sets"),
        help="路径集合输出根目录",
    )
    parser.add_argument(
        "--trip-dir",
        type=Path,
        default=Path("trip"),
        help="OD Shapefile所在目录",
    )
    parser.add_argument(
        "--road-file",
        type=Path,
        default=Path("beijing/2017年北京市道路数据.shp"),
        help="道路中心线Shapefile",
    )
    parser.add_argument(
        "--projected-crs",
        default="EPSG:32650",
        help="路径计算使用的米制投影坐标系",
    )
    parser.add_argument(
        "--max-snap-m",
        type=float,
        default=500.0,
        help="OD到道路中心线的最大允许吸附距离（米）",
    )
    parser.add_argument("--max-ratio", type=float, default=1.20)
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=5,
        help="寻找随机替代路径的最大尝试次数",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        help="并行进程数；默认使用全部逻辑CPU",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="完成后逐个回读Shapefile验收（全量时较慢）",
    )
    parser.add_argument(
        "--shard-count",
        type=int,
        default=1,
        help="分布式实例总数",
    )
    parser.add_argument(
        "--shard-index",
        type=int,
        default=0,
        help="当前实例分片编号，从0开始",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.shard_count < 1:
        raise ValueError("--shard-count 必须至少为1")
    if not 0 <= args.shard_index < args.shard_count:
        raise ValueError("--shard-index 必须位于 [0, shard-count) 范围")
    route_config = RouteConfig(
        trip_dir=args.trip_dir,
        road_file=args.road_file,
        projected_crs=args.projected_crs,
        max_snap_m=args.max_snap_m,
    )
    set_config = PathSetConfig(
        max_ratio=args.max_ratio,
        max_attempts=args.max_attempts,
        output_root=args.output_root,
    )

    trips = load_trips(route_config, limit=args.limit)
    if args.shard_count > 1:
        trips = trips.iloc[args.shard_index :: args.shard_count].copy()
    roads = load_roads(route_config)
    raw_graph = build_graph(roads)
    routing_roads, routing_multigraph = retain_largest_component(roads, raw_graph)
    graph = simplify_graph(routing_multigraph)

    print(f"本次OD: {len(trips):,}")
    print(f"分片: {args.shard_index + 1}/{args.shard_count}")
    print(f"路网节点: {graph.number_of_nodes():,}")
    print(f"有向简单边: {graph.number_of_edges():,}")
    print(f"长度比例上限: {set_config.max_ratio:.0%}")
    print(f"每个ID最多路径: 2")
    print(f"随机替代路最多尝试: {set_config.max_attempts}")

    print(f"并行进程: {args.workers}")
    index = generate_path_sets_parallel(
        trips,
        routing_roads,
        graph,
        route_config,
        set_config,
        workers=args.workers,
    )
    if args.validate:
        validate_path_set_output(index, set_config)

    print(f"完成ID: {len(index):,}")
    print(f"成功集合: {index['status'].eq('ok').sum():,}")
    print(f"候选路径总数: {index['path_count'].sum():,}")
    print(f"状态统计: {index['status'].value_counts().to_dict()}")
    print(f"总索引: {(set_config.output_root / 'path_set_index.csv').resolve()}")


if __name__ == "__main__":
    main()
