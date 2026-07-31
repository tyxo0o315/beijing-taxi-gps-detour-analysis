"""Generate a near-optimal path set for every one-to-one taxi OD."""

from __future__ import annotations

from dataclasses import dataclass
import multiprocessing as mp
import os
from pathlib import Path
import random

import geopandas as gpd
import networkx as nx
import pandas as pd
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge
from tqdm.auto import tqdm

from shortest_route_pipeline import (
    RouteConfig,
    _road_info,
    _same_road_candidate,
    _source_options,
    _target_options,
    make_od_points,
    snap_to_roads,
)


@dataclass(frozen=True)
class PathSetConfig:
    max_ratio: float = 1.20
    max_attempts: int = 5
    max_paths: int = 2
    output_root: Path = Path("output/path_sets")

    def __post_init__(self) -> None:
        if self.max_ratio < 1.0:
            raise ValueError("max_ratio 必须不小于1")
        if self.max_attempts < 1:
            raise ValueError("max_attempts 必须为正整数")
        if self.max_paths != 2:
            raise ValueError("双路径算法的max_paths必须为2")


def _merge_ordered(parts):
    valid = [
        part
        for part in parts
        if part is not None and not part.is_empty and getattr(part, "length", 0) > 0
    ]
    if not valid:
        return None
    merged = linemerge(MultiLineString(valid))
    return merged


def _edge_signature(node_path: list) -> tuple:
    return tuple(zip(node_path[:-1], node_path[1:]))


def _route_parts(graph: nx.DiGraph, node_path: list) -> list[LineString]:
    return [
        graph.edges[u, v].get("geometry")
        for u, v in zip(node_path[:-1], node_path[1:])
    ]


def enumerate_one_path_set(
    graph: nx.DiGraph,
    source_info: dict,
    source_position: float,
    target_info: dict,
    target_position: float,
    max_ratio: float,
    max_attempts: int,
) -> list[dict]:
    """Return the strict shortest path and one random edge-exclusion alternative."""
    source_options = _source_options(source_info, source_position)
    target_options = _target_options(target_info, target_position)

    def best_path(banned_edge: tuple | None = None) -> dict | None:
        best = None

        if source_info["road_idx"] == target_info["road_idx"]:
            direct = _same_road_candidate(
                source_info, source_position, target_position
            )
            if direct is not None and direct[0] > 1e-9:
                geometry = _merge_ordered([direct[1]])
                if geometry is not None and not geometry.is_empty:
                    best = {
                        "dist_m": float(direct[0]),
                        "n_edges": 1,
                        "geometry": geometry,
                        "edge_sig": (
                            (
                                "direct",
                                source_info["road_idx"],
                                source_position,
                                target_position,
                            ),
                        ),
                        "core_edges": [],
                    }

        def edge_weight(u, v, data):
            if banned_edge is not None and (u, v) == banned_edge:
                return None
            return data["length"]

        weight = "length" if banned_edge is None else edge_weight
        for source_node, source_cost, source_geom in source_options:
            for target_node, target_cost, target_geom in target_options:
                try:
                    node_path = nx.shortest_path(
                        graph,
                        source_node,
                        target_node,
                        weight=weight,
                        method="dijkstra",
                    )
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
                core_edges = list(_edge_signature(node_path))
                core_cost = sum(
                    float(graph.edges[u, v]["length"]) for u, v in core_edges
                )
                distance = float(source_cost) + core_cost + float(target_cost)
                if best is not None and distance >= best["dist_m"] - 1e-9:
                    continue
                geometry = _merge_ordered(
                    [
                        source_geom,
                        *_route_parts(graph, node_path),
                        target_geom,
                    ]
                )
                if geometry is None or geometry.is_empty:
                    continue
                best = {
                    "dist_m": distance,
                    "n_edges": len(node_path) + 1,
                    "geometry": geometry,
                    "edge_sig": (
                        ("source", source_node),
                        *core_edges,
                        ("target", target_node),
                    ),
                    "core_edges": core_edges,
                }
        return best

    shortest = best_path()
    if shortest is None:
        return []
    minimum = shortest["dist_m"]
    shortest["ratio"] = 1.0
    selected = [shortest]

    edges_to_try = list(dict.fromkeys(shortest["core_edges"]))
    random.SystemRandom().shuffle(edges_to_try)
    for banned_edge in edges_to_try[:max_attempts]:
        alternative = best_path(banned_edge)
        if alternative is None:
            continue
        if alternative["edge_sig"] == shortest["edge_sig"]:
            continue
        if alternative["dist_m"] > minimum * max_ratio + 1e-7:
            continue
        alternative["ratio"] = float(alternative["dist_m"] / minimum)
        selected.append(alternative)
        break

    for candidate in selected:
        candidate.pop("core_edges", None)
    return selected


def _write_status(folder: Path, status: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([status]).to_csv(
        folder / "status.csv", index=False, encoding="utf-8-sig"
    )


def export_one_path_set(
    records: list[dict],
    folder: Path,
    crs: str,
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "paths.shp"
    columns = [
        "route_id",
        "alt_rank",
        "dist_m",
        "ratio",
        "is_best",
        "n_edges",
        "snap_s_m",
        "snap_e_m",
        "status",
        "geometry",
    ]
    frame = gpd.GeoDataFrame(records, geometry="geometry", crs=crs)
    frame[columns].to_file(path, driver="ESRI Shapefile", encoding="UTF-8")
    return path


def generate_path_sets(
    trips: gpd.GeoDataFrame,
    routing_roads: gpd.GeoDataFrame,
    graph: nx.DiGraph,
    route_config: RouteConfig,
    set_config: PathSetConfig,
    *,
    write_index: bool = True,
    show_progress: bool = True,
) -> pd.DataFrame:
    """Generate and immediately export one Shapefile path set per route_id."""
    starts, ends = make_od_points(trips, route_config.projected_crs)
    start_snap = snap_to_roads(starts, routing_roads)
    end_snap = snap_to_roads(ends, routing_roads)
    roads_by_idx = routing_roads.set_index("road_idx")
    index_records = []

    for position, (_, trip) in enumerate(
        tqdm(
            trips.iterrows(),
            total=len(trips),
            desc="生成OD路径集合",
            disable=not show_progress,
        )
    ):
        route_id = str(trip["route_id"])
        trip_date = str(trip["trip_date"])
        folder = set_config.output_root / trip_date.replace("-", "") / route_id
        ss = start_snap.iloc[position]
        es = end_snap.iloc[position]
        common = {
            "route_id": route_id,
            "taxi_id": str(trip["taxi_id"]),
            "trip_date": trip_date,
            "trip_id": int(trip["trip_id"]),
            "start_lon": float(trip["start_lon"]),
            "start_lat": float(trip["start_lat"]),
            "end_lon": float(trip["end_lon"]),
            "end_lat": float(trip["end_lat"]),
            "snap_s_m": float(ss.snap_m),
            "snap_e_m": float(es.snap_m),
        }
        status = "ok"
        alt_status = "not_applicable"
        message = ""
        path_count = 0
        minimum = None

        if ss.snap_m > route_config.max_snap_m or es.snap_m > route_config.max_snap_m:
            status = "snap_far"
            message = "起点或终点离机动车路网过远"
            candidates = []
        elif (
            float(trip["start_lon"]) == float(trip["end_lon"])
            and float(trip["start_lat"]) == float(trip["end_lat"])
        ):
            status = "zero_distance"
            message = "起点与终点完全相同，最短距离为0"
            candidates = []
            minimum = 0.0
        elif ss.point.distance(es.point) <= 1e-6:
            status = "empty_geom"
            message = "起终点吸附到同一位置，无法形成正长度线"
            candidates = []
            minimum = 0.0
        else:
            source_info = _road_info(roads_by_idx, int(ss.road_idx))
            target_info = _road_info(roads_by_idx, int(es.road_idx))
            source_info["road_idx"] = int(ss.road_idx)
            target_info["road_idx"] = int(es.road_idx)
            candidates = enumerate_one_path_set(
                graph,
                source_info,
                float(ss.position),
                target_info,
                float(es.position),
                set_config.max_ratio,
                set_config.max_attempts,
            )
            if not candidates:
                status = "no_path"
                message = "路网中没有满足条件的合法简单路径"

        if candidates:
            minimum = candidates[0]["dist_m"]
            alt_status = "ok" if len(candidates) == 2 else "no_valid_alternative"
            if alt_status == "no_valid_alternative":
                message = f"随机禁止最短路边最多{set_config.max_attempts}次，未找到120%以内的不同路径"
            records = []
            for rank, candidate in enumerate(candidates, start=1):
                records.append(
                    {
                        "route_id": route_id,
                        "alt_rank": rank,
                        "dist_m": candidate["dist_m"],
                        "ratio": candidate["ratio"],
                        "is_best": rank == 1,
                        "n_edges": candidate["n_edges"],
                        "snap_s_m": common["snap_s_m"],
                        "snap_e_m": common["snap_e_m"],
                        "status": "ok",
                        "geometry": candidate["geometry"],
                    }
                )
            export_one_path_set(records, folder, route_config.projected_crs)
            path_count = len(records)

        status_record = {
            **common,
            "status": status,
            "message": message,
            "min_dist_m": minimum,
            "path_count": path_count,
            "max_ratio": set_config.max_ratio,
            "max_attempts": set_config.max_attempts,
            "max_paths": set_config.max_paths,
            "alt_status": alt_status,
        }
        _write_status(folder, status_record)
        index_records.append(
            {
                **status_record,
                "output_dir": str(folder),
            }
        )

    index = pd.DataFrame(index_records)
    if write_index:
        set_config.output_root.mkdir(parents=True, exist_ok=True)
        index.to_csv(
            set_config.output_root / "path_set_index.csv",
            index=False,
            encoding="utf-8-sig",
        )
    return index


_PARALLEL_CONTEXT = {}


def _prepare_parallel_tasks(
    trips: gpd.GeoDataFrame,
    routing_roads: gpd.GeoDataFrame,
    route_config: RouteConfig,
) -> list[dict]:
    starts, ends = make_od_points(trips, route_config.projected_crs)
    start_snap = snap_to_roads(starts, routing_roads)
    end_snap = snap_to_roads(ends, routing_roads)
    roads_by_idx = routing_roads.set_index("road_idx")
    tasks = []
    for position, (_, trip) in enumerate(trips.iterrows()):
        ss = start_snap.iloc[position]
        es = end_snap.iloc[position]
        source_info = _road_info(roads_by_idx, int(ss.road_idx))
        target_info = _road_info(roads_by_idx, int(es.road_idx))
        source_info["road_idx"] = int(ss.road_idx)
        target_info["road_idx"] = int(es.road_idx)
        tasks.append(
            {
                "trip": trip.drop(labels=["geometry"]).to_dict(),
                "ss": {
                    "snap_m": float(ss.snap_m),
                    "position": float(ss.position),
                    "point": ss.point,
                },
                "es": {
                    "snap_m": float(es.snap_m),
                    "position": float(es.position),
                    "point": es.point,
                },
                "source_info": source_info,
                "target_info": target_info,
            }
        )
    return tasks


def _process_parallel_task(task: dict) -> dict:
    context = _PARALLEL_CONTEXT
    route_config = context["route_config"]
    set_config = context["set_config"]
    graph = context["graph"]
    trip = task["trip"]
    ss = task["ss"]
    es = task["es"]
    route_id = str(trip["route_id"])
    trip_date = str(trip["trip_date"])
    folder = set_config.output_root / trip_date.replace("-", "") / route_id
    common = {
        "route_id": route_id,
        "taxi_id": str(trip["taxi_id"]),
        "trip_date": trip_date,
        "trip_id": int(trip["trip_id"]),
        "start_lon": float(trip["start_lon"]),
        "start_lat": float(trip["start_lat"]),
        "end_lon": float(trip["end_lon"]),
        "end_lat": float(trip["end_lat"]),
        "snap_s_m": float(ss["snap_m"]),
        "snap_e_m": float(es["snap_m"]),
    }
    status = "ok"
    alt_status = "not_applicable"
    message = ""
    candidates = []
    minimum = None

    if ss["snap_m"] > route_config.max_snap_m or es["snap_m"] > route_config.max_snap_m:
        status = "snap_far"
        message = "起点或终点离机动车路网过远"
    elif (
        common["start_lon"] == common["end_lon"]
        and common["start_lat"] == common["end_lat"]
    ):
        status = "zero_distance"
        message = "起点与终点完全相同，最短距离为0"
        minimum = 0.0
    elif ss["point"].distance(es["point"]) <= 1e-6:
        status = "empty_geom"
        message = "起终点吸附到同一位置，无法形成正长度线"
        minimum = 0.0
    else:
        candidates = enumerate_one_path_set(
            graph,
            task["source_info"],
            ss["position"],
            task["target_info"],
            es["position"],
            set_config.max_ratio,
            set_config.max_attempts,
        )
        if not candidates:
            status = "no_path"
            message = "路网中没有满足条件的合法简单路径"

    if candidates:
        minimum = candidates[0]["dist_m"]
        alt_status = "ok" if len(candidates) == 2 else "no_valid_alternative"
        if alt_status == "no_valid_alternative":
            message = f"随机禁止最短路边最多{set_config.max_attempts}次，未找到120%以内的不同路径"
        records = [
            {
                "route_id": route_id,
                "alt_rank": rank,
                "dist_m": candidate["dist_m"],
                "ratio": candidate["ratio"],
                "is_best": rank == 1,
                "n_edges": candidate["n_edges"],
                "snap_s_m": common["snap_s_m"],
                "snap_e_m": common["snap_e_m"],
                "status": "ok",
                "geometry": candidate["geometry"],
            }
            for rank, candidate in enumerate(candidates, start=1)
        ]
        export_one_path_set(records, folder, route_config.projected_crs)
    status_record = {
        **common,
        "status": status,
        "message": message,
        "min_dist_m": minimum,
        "path_count": len(candidates),
        "max_ratio": set_config.max_ratio,
        "max_attempts": set_config.max_attempts,
        "max_paths": set_config.max_paths,
        "alt_status": alt_status,
    }
    _write_status(folder, status_record)
    return {**status_record, "output_dir": str(folder)}


def generate_path_sets_parallel(
    trips: gpd.GeoDataFrame,
    routing_roads: gpd.GeoDataFrame,
    graph: nx.DiGraph,
    route_config: RouteConfig,
    set_config: PathSetConfig,
    workers: int | None = None,
) -> pd.DataFrame:
    """Fork workers that share the read-only graph and export independent OD folders."""
    if workers is None:
        workers = os.cpu_count() or 1
    workers = max(1, min(int(workers), len(trips)))
    if workers == 1:
        return generate_path_sets(
            trips, routing_roads, graph, route_config, set_config
        )
    if "fork" not in mp.get_all_start_methods():
        raise RuntimeError("并行加速需要支持fork的操作系统")

    _PARALLEL_CONTEXT.clear()
    tasks = _prepare_parallel_tasks(trips, routing_roads, route_config)
    _PARALLEL_CONTEXT.update(
        {
            "graph": graph,
            "route_config": route_config,
            "set_config": set_config,
        }
    )
    context = mp.get_context("fork")
    with context.Pool(processes=workers) as pool:
        records = list(
            tqdm(
                pool.imap_unordered(_process_parallel_task, tasks, chunksize=1),
                total=len(tasks),
                desc=f"{workers}进程动态并行",
            )
        )
    _PARALLEL_CONTEXT.clear()

    index = pd.DataFrame(records).sort_values(
        ["trip_date", "trip_id"], kind="stable"
    )
    set_config.output_root.mkdir(parents=True, exist_ok=True)
    index.to_csv(
        set_config.output_root / "path_set_index.csv",
        index=False,
        encoding="utf-8-sig",
    )
    return index


def validate_path_set_output(
    index: pd.DataFrame,
    set_config: PathSetConfig,
) -> None:
    if index["route_id"].duplicated().any():
        raise AssertionError("path_set_index中的route_id不唯一")
    for row in index.loc[index["status"].eq("ok")].itertuples():
        path = Path(row.output_dir) / "paths.shp"
        frame = gpd.read_file(path)
        if len(frame) != row.path_count:
            raise AssertionError(f"{row.route_id}: 路径数量不一致")
        if not (1 <= len(frame) <= set_config.max_paths):
            raise AssertionError(f"{row.route_id}: 路径数量越界")
        if frame["alt_rank"].tolist() != list(range(1, len(frame) + 1)):
            raise AssertionError(f"{row.route_id}: alt_rank不连续")
        if not frame["dist_m"].is_monotonic_increasing:
            raise AssertionError(f"{row.route_id}: 距离未升序")
        minimum = frame["dist_m"].iloc[0]
        if (frame["dist_m"] > minimum * set_config.max_ratio + 1e-5).any():
            raise AssertionError(f"{row.route_id}: 存在超过比例阈值的路径")
        if frame.geometry.isna().any() or frame.geometry.is_empty.any():
            raise AssertionError(f"{row.route_id}: 存在空几何")
