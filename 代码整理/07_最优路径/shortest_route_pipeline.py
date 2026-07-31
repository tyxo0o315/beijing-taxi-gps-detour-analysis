"""Shortest-distance routing pipeline for the Beijing taxi OD datasets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import networkx as nx
import pandas as pd
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge, substring
from tqdm.auto import tqdm


DRIVABLE_CLASSES = {
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
    "primary",
    "primary_link",
    "secondary",
    "secondary_link",
    "tertiary",
    "tertiary_link",
    "residential",
    "unclassified",
    "living_street",
    "service",
}


@dataclass(frozen=True)
class RouteConfig:
    trip_dir: Path = Path("trip")
    road_file: Path = Path("beijing/2017年北京市道路数据.shp")
    output_dir: Path = Path("output")
    projected_crs: str = "EPSG:32650"
    max_snap_m: float = 500.0
    drivable_classes: frozenset[str] = frozenset(DRIVABLE_CLASSES)


def _trip_date(path: Path) -> str:
    match = re.search(r"(?<!\d)(\d{2})(\d{2})(?!\d)", path.stem)
    if not match:
        raise ValueError(f"无法从文件名提取日期: {path.name}")
    month, day = map(int, match.groups())
    date = pd.Timestamp(year=2017, month=month, day=day)
    return date.strftime("%Y-%m-%d")


def load_trips(config: RouteConfig, limit: int | None = None) -> gpd.GeoDataFrame:
    """Read all daily OD layers and create globally unique route IDs."""
    files = sorted(config.trip_dir.glob("trip*.shp"))
    if not files:
        raise FileNotFoundError(f"未找到 OD Shapefile: {config.trip_dir}")

    frames = []
    required = {
        "trip_id",
        "taxi_id",
        "start_lon",
        "start_lat",
        "end_lon",
        "end_lat",
        "is_plausib",
    }
    for path in files:
        frame = gpd.read_file(path, encoding="UTF-8")
        missing = required.difference(frame.columns)
        if missing:
            raise KeyError(f"{path.name} 缺少字段: {sorted(missing)}")
        date = _trip_date(path)
        frame["trip_date"] = date
        frame["trip_id"] = pd.to_numeric(frame["trip_id"], errors="coerce").astype("Int64")
        frame["route_id"] = (
            date.replace("-", "")
            + "_"
            + frame["trip_id"].astype("string")
        )
        frames.append(frame)

    trips = pd.concat(frames, ignore_index=True)
    numeric = ["start_lon", "start_lat", "end_lon", "end_lat"]
    for column in numeric:
        trips[column] = pd.to_numeric(trips[column], errors="coerce")

    plausible = (
        trips["is_plausib"]
        .astype(str)
        .str.strip()
        .str.lower()
        .isin({"true", "1", "yes", "y", "t"})
    )
    trips = trips.loc[plausible].dropna(subset=numeric + ["trip_id", "taxi_id"]).copy()
    trips = trips.drop_duplicates("route_id", keep="first")
    if limit is not None:
        trips = trips.head(limit).copy()

    result = gpd.GeoDataFrame(trips, geometry="geometry", crs=frames[0].crs)
    if result.crs is None:
        result = result.set_crs("EPSG:4326")
    if result["route_id"].duplicated().any():
        raise AssertionError("route_id 不是全局唯一")
    return result


def load_roads(config: RouteConfig) -> gpd.GeoDataFrame:
    """Read, filter, project, and measure the motor-vehicle road network."""
    roads = gpd.read_file(config.road_file, encoding="GB2312")
    required = {"osm_id", "fclass", "oneway", "layer", "bridge", "tunnel", "geometry"}
    missing = required.difference(roads.columns)
    if missing:
        raise KeyError(f"路网缺少字段: {sorted(missing)}")
    if roads.crs is None:
        raise ValueError("路网缺少 CRS")

    roads = roads.loc[
        roads.geometry.notna()
        & ~roads.geometry.is_empty
        & roads["fclass"].isin(config.drivable_classes)
    ].copy()
    roads = roads.explode(index_parts=False, ignore_index=True)
    roads = roads.loc[roads.geom_type.eq("LineString")].copy()
    roads = roads.to_crs(config.projected_crs)
    roads["length_m"] = roads.geometry.length
    roads = roads.loc[roads["length_m"].gt(0)].reset_index(drop=True)
    roads["road_idx"] = roads.index.astype(int)
    return roads


def _node_key(coord: Iterable[float], precision: int = 3) -> tuple[float, float]:
    x, y = coord
    return round(float(x), precision), round(float(y), precision)


def _reverse_line(line: LineString) -> LineString:
    return LineString(list(line.coords)[::-1])


def build_graph(roads: gpd.GeoDataFrame) -> nx.MultiDiGraph:
    """Build a directed graph from every consecutive pair of road vertices."""
    graph = nx.MultiDiGraph()
    for row in tqdm(roads.itertuples(), total=len(roads), desc="构建路网图"):
        coords = list(row.geometry.coords)
        direction = str(row.oneway).strip().upper()
        if direction not in {"B", "F", "T", "", "NONE", "NAN"}:
            raise ValueError(f"未知 oneway 编码: {row.oneway!r}")
        for start, end in zip(coords[:-1], coords[1:]):
            segment = LineString([start, end])
            segment_length = float(segment.length)
            if segment_length <= 0:
                continue
            u = _node_key(start)
            v = _node_key(end)
            common = {
                "length": segment_length,
                "road_idx": int(row.road_idx),
                "osm_id": str(row.osm_id),
                "layer": row.layer,
                "bridge": row.bridge,
                "tunnel": row.tunnel,
            }
            if direction in {"B", "", "NONE", "NAN"}:
                graph.add_edge(u, v, geometry=segment, **common)
                graph.add_edge(v, u, geometry=_reverse_line(segment), **common)
            elif direction == "F":
                graph.add_edge(u, v, geometry=segment, **common)
            else:
                graph.add_edge(v, u, geometry=_reverse_line(segment), **common)
    return graph


def retain_largest_component(
    roads: gpd.GeoDataFrame, graph: nx.MultiDiGraph
) -> tuple[gpd.GeoDataFrame, nx.MultiDiGraph]:
    """Keep the largest weakly connected motor-road component for routing/snapping."""
    components = nx.weakly_connected_components(graph)
    largest_nodes = max(components, key=len)
    routing_graph = graph.subgraph(largest_nodes).copy()

    segment_records = []
    for row in roads.itertuples():
        coords = list(row.geometry.coords)
        for start, end in zip(coords[:-1], coords[1:]):
            if _node_key(start) not in largest_nodes or _node_key(end) not in largest_nodes:
                continue
            segment = LineString([start, end])
            if segment.length <= 0:
                continue
            segment_records.append(
                {
                    "osm_id": str(row.osm_id),
                    "fclass": row.fclass,
                    "oneway": row.oneway,
                    "layer": row.layer,
                    "bridge": row.bridge,
                    "tunnel": row.tunnel,
                    "length_m": float(segment.length),
                    "geometry": segment,
                }
            )
    routing_roads = gpd.GeoDataFrame(
        segment_records, geometry="geometry", crs=roads.crs
    ).reset_index(drop=True)
    routing_roads["road_idx"] = routing_roads.index.astype(int)
    return routing_roads, routing_graph


def simplify_graph(graph: nx.MultiDiGraph) -> nx.DiGraph:
    """Collapse parallel directed edges, retaining the shortest legal edge."""
    simple = nx.DiGraph()
    for u, v, attrs in tqdm(
        graph.edges(data=True), total=graph.number_of_edges(), desc="简化有向路网"
    ):
        current = simple.get_edge_data(u, v)
        if current is None or attrs["length"] < current["length"]:
            simple.add_edge(u, v, **attrs)
    return simple


def make_od_points(
    trips: gpd.GeoDataFrame, projected_crs: str
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    starts = gpd.GeoDataFrame(
        trips[["route_id"]].copy(),
        geometry=gpd.points_from_xy(trips["start_lon"], trips["start_lat"]),
        crs="EPSG:4326",
    ).to_crs(projected_crs)
    ends = gpd.GeoDataFrame(
        trips[["route_id"]].copy(),
        geometry=gpd.points_from_xy(trips["end_lon"], trips["end_lat"]),
        crs="EPSG:4326",
    ).to_crs(projected_crs)
    return starts, ends


def snap_to_roads(
    points: gpd.GeoDataFrame, roads: gpd.GeoDataFrame
) -> pd.DataFrame:
    """Snap each point to the nearest retained motor-vehicle road."""
    joined = gpd.sjoin_nearest(
        points,
        roads[["road_idx", "geometry"]],
        how="left",
        distance_col="snap_m",
    )
    joined = joined.loc[~joined.index.duplicated(keep="first")].sort_index()
    road_lookup = roads.set_index("road_idx")
    records = []
    for item in joined.itertuples():
        road_idx = int(item.road_idx)
        line = road_lookup.loc[road_idx].geometry
        position = float(line.project(item.geometry))
        records.append(
            {
                "road_idx": road_idx,
                "snap_m": float(item.snap_m),
                "position": position,
                "point": line.interpolate(position),
            }
        )
    return pd.DataFrame(records, index=joined.index)


def _road_info(roads_by_idx: gpd.GeoDataFrame, road_idx: int) -> dict:
    row = roads_by_idx.loc[road_idx]
    line = row.geometry
    coords = list(line.coords)
    return {
        "line": line,
        "length": float(row.length_m),
        "coords": coords,
        "direction": str(row.oneway).strip().upper(),
    }


def _vertex_positions(line: LineString, coords: list[tuple]) -> list[float]:
    positions = [0.0]
    total = 0.0
    for start, end in zip(coords[:-1], coords[1:]):
        total += LineString([start, end]).length
        positions.append(total)
    positions[-1] = line.length
    return positions


def _bracket(info: dict, position: float) -> tuple[int, list[float]]:
    positions = _vertex_positions(info["line"], info["coords"])
    for index in range(len(positions) - 1):
        if position <= positions[index + 1] + 1e-9:
            return index, positions
    return len(positions) - 2, positions


def _line_part(line: LineString, start: float, end: float, reverse: bool = False):
    if abs(end - start) <= 1e-9:
        return None
    part = substring(line, min(start, end), max(start, end))
    if reverse:
        part = _reverse_line(part)
    return part


def _source_options(info: dict, position: float) -> list[tuple[tuple, float, LineString]]:
    line, coords, direction = (
        info["line"],
        info["coords"],
        info["direction"],
    )
    index, positions = _bracket(info, position)
    prev_node = _node_key(coords[index])
    next_node = _node_key(coords[index + 1])
    prev_pos, next_pos = positions[index], positions[index + 1]
    options = []
    if direction in {"B", "", "NONE", "NAN", "T"}:
        options.append(
            (prev_node, position - prev_pos, _line_part(line, prev_pos, position, True))
        )
    if direction in {"B", "", "NONE", "NAN", "F"}:
        options.append(
            (next_node, next_pos - position, _line_part(line, position, next_pos))
        )
    return options


def _target_options(info: dict, position: float) -> list[tuple[tuple, float, LineString]]:
    line, coords, direction = (
        info["line"],
        info["coords"],
        info["direction"],
    )
    index, positions = _bracket(info, position)
    prev_node = _node_key(coords[index])
    next_node = _node_key(coords[index + 1])
    prev_pos, next_pos = positions[index], positions[index + 1]
    options = []
    if direction in {"B", "", "NONE", "NAN", "F"}:
        options.append(
            (prev_node, position - prev_pos, _line_part(line, prev_pos, position))
        )
    if direction in {"B", "", "NONE", "NAN", "T"}:
        options.append(
            (next_node, next_pos - position, _line_part(line, position, next_pos, True))
        )
    return options


def _same_road_candidate(
    info: dict, start_pos: float, end_pos: float
) -> tuple[float, LineString] | None:
    direction = info["direction"]
    if end_pos >= start_pos and direction in {"B", "", "NONE", "NAN", "F"}:
        return end_pos - start_pos, _line_part(info["line"], start_pos, end_pos)
    if start_pos >= end_pos and direction in {"B", "", "NONE", "NAN", "T"}:
        line = _line_part(info["line"], end_pos, start_pos, True)
        return start_pos - end_pos, line
    return None


def _shortest_edge(graph: nx.MultiDiGraph, u: tuple, v: tuple) -> dict:
    choices = graph.get_edge_data(u, v)
    if not choices:
        raise KeyError(f"图中缺少边 {u} -> {v}")
    return min(choices.values(), key=lambda attrs: attrs["length"])


def _merge_ordered(parts: list[LineString]):
    valid = [part for part in parts if part is not None and not part.is_empty and part.length > 0]
    if not valid:
        return None
    merged = linemerge(MultiLineString(valid))
    return merged


def solve_routes(
    trips: gpd.GeoDataFrame,
    roads: gpd.GeoDataFrame,
    graph: nx.MultiDiGraph,
    config: RouteConfig,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """Solve shortest legal road distance for every OD."""
    starts, ends = make_od_points(trips, config.projected_crs)
    start_snap = snap_to_roads(starts, roads)
    end_snap = snap_to_roads(ends, roads)
    roads_by_idx = roads.set_index("road_idx")

    successes = []
    failures = []
    for pos, (_, trip) in enumerate(
        tqdm(trips.iterrows(), total=len(trips), desc="计算最短路径")
    ):
        route_id = str(trip["route_id"])
        ss = start_snap.iloc[pos]
        es = end_snap.iloc[pos]
        base = {
            "route_id": route_id,
            "taxi_id": str(trip["taxi_id"]),
            "trip_date": str(trip["trip_date"]),
            "trip_id": int(trip["trip_id"]),
            "snap_s_m": float(ss.snap_m),
            "snap_e_m": float(es.snap_m),
        }
        if ss.snap_m > config.max_snap_m or es.snap_m > config.max_snap_m:
            failures.append(
                {**base, "status": "snap_far", "message": "起点或终点离机动车路网过远"}
            )
            continue

        source_info = _road_info(roads_by_idx, int(ss.road_idx))
        target_info = _road_info(roads_by_idx, int(es.road_idx))
        candidates = []
        if int(ss.road_idx) == int(es.road_idx):
            direct = _same_road_candidate(source_info, ss.position, es.position)
            if direct is not None:
                candidates.append((direct[0], [direct[1]], 1))

        for source_node, source_cost, source_geom in _source_options(
            source_info, ss.position
        ):
            for target_node, target_cost, target_geom in _target_options(
                target_info, es.position
            ):
                try:
                    graph_cost, node_path = nx.bidirectional_dijkstra(
                        graph, source_node, target_node, weight="length"
                    )
                except nx.NetworkXNoPath:
                    continue
                edge_geoms = []
                for u, v in zip(node_path[:-1], node_path[1:]):
                    edge_geoms.append(_shortest_edge(graph, u, v)["geometry"])
                candidates.append(
                    (
                        source_cost + graph_cost + target_cost,
                        [source_geom, *edge_geoms, target_geom],
                        len(edge_geoms) + 2,
                    )
                )

        if not candidates:
            failures.append({**base, "status": "no_path", "message": "路网中无合法路径"})
            continue
        distance, parts, n_edges = min(candidates, key=lambda candidate: candidate[0])
        geometry = _merge_ordered(parts)
        if geometry is None or geometry.is_empty:
            failures.append({**base, "status": "empty_geom", "message": "路径几何为空"})
            continue
        successes.append(
            {
                **base,
                "dist_m": float(distance),
                "n_edges": int(n_edges),
                "status": "ok",
                "geometry": geometry,
            }
        )

    routes = gpd.GeoDataFrame(successes, geometry="geometry", crs=config.projected_crs)
    return routes, pd.DataFrame(failures)


def validate_routes(routes: gpd.GeoDataFrame) -> None:
    if routes.empty:
        raise AssertionError("没有成功路线")
    if routes.crs is None:
        raise AssertionError("结果缺少 CRS")
    if routes["route_id"].duplicated().any():
        raise AssertionError("结果 route_id 不唯一")
    if routes.geometry.isna().any() or routes.geometry.is_empty.any():
        raise AssertionError("结果存在空几何")
    if (~routes.geometry.is_valid).any():
        raise AssertionError("结果存在无效几何")
    if (routes["dist_m"] <= 0).any():
        raise AssertionError("结果存在非正距离")


def export_results(
    routes: gpd.GeoDataFrame,
    failures: pd.DataFrame,
    config: RouteConfig,
    suffix: str = "",
) -> tuple[Path, Path]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    shp_path = config.output_dir / f"optimal_routes{suffix}.shp"
    failed_path = config.output_dir / f"failed_routes{suffix}.csv"
    columns = [
        "route_id",
        "taxi_id",
        "trip_date",
        "trip_id",
        "dist_m",
        "snap_s_m",
        "snap_e_m",
        "n_edges",
        "status",
        "geometry",
    ]
    routes[columns].to_file(shp_path, driver="ESRI Shapefile", encoding="UTF-8")
    failures.to_csv(failed_path, index=False, encoding="utf-8-sig")
    return shp_path, failed_path
