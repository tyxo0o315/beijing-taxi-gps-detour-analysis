r"""
拥堵判别分析——把"速度<32km/h持续超过3分钟=拥堵事件"这条规则应用到轨迹上，识别拥堵
事件，再按4级速度等级分类(畅通/轻度拥挤/拥挤/严重拥挤)，最后按六边形格子+小时聚合。

判定规则(用户给定，不含时间占有率——占有率是基于固定断面检测器连续监测的概念，
出租车GPS是稀疏采样的浮动车数据，不适用这套定义，讨论后明确不用)
--------------------------------------------------------------
1. 拥堵事件识别：沿单车连续轨迹(不跨trip_break/bad_coord)，找"speed_gps_kmh<32km/h"
   连续持续超过3分钟(180秒)的路段，整段标记为一次拥堵事件
2. 4级拥堵等级(按速度分级，畅通/轻度拥挤/拥挤/严重拥挤对应速度>30/20~30/10~20/<10 km/h)，
   应用在: (a)每个拥堵事件自身的平均速度 (b)每个"格子+小时"的整体平均速度(不要求
   先识别出拥堵事件才分级，分级是对速度的直接快照)

用法:
    python 17_congestion_analysis.py
    python 17_congestion_analysis.py --hex-size 300 --min-duration-sec 180 --speed-threshold 32
"""
import argparse
import datetime
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import FEATURES_CSV, ensure_out_dir
from kde_utils import lonlat_to_utm
from hex_utils import bin_points_to_hex

COL_TS, COL_LAT, COL_LON = 2, 3, 4
COL_SPEED_GPS, COL_TRIP_BREAK, COL_BAD_COORD = 9, 12, 13

_CST = datetime.timezone(datetime.timedelta(hours=8))


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", default=FEATURES_CSV)
    p.add_argument("--events-out", default=None, help="拥堵事件明细输出路径，默认output/congestion_events.csv")
    p.add_argument("--hex-out", default=None, help="按格子+小时聚合的输出路径，默认output/congestion_by_hex_hour.gpkg")
    p.add_argument("--speed-threshold", type=float, default=32.0, help="低于这个速度(km/h)算拥堵候选，默认32")
    p.add_argument("--min-duration-sec", type=int, default=180, help="持续超过多少秒才算一次拥堵事件，默认180(3分钟)")
    p.add_argument("--hex-size", type=float, default=300.0)
    p.add_argument("--limit", type=int, default=None)
    return p.parse_args()


def classify_congestion_level(speed_kmh):
    """4级拥堵等级：畅通>30，轻度拥挤20~30，拥挤10~20，严重拥挤<10 (km/h)。"""
    if speed_kmh is None:
        return ""
    if speed_kmh > 30:
        return "畅通"
    if speed_kmh > 20:
        return "轻度拥挤"
    if speed_kmh > 10:
        return "拥挤"
    return "严重拥挤"


def hour_of_day(ts):
    return datetime.datetime.fromtimestamp(ts, tz=_CST).hour


EVENT_HEADER = ["taxi_id", "start_ts", "end_ts", "duration_sec", "avg_speed_kmh",
                "congestion_level", "start_lat", "start_lon", "end_lat", "end_lon",
                "hex_q", "hex_r", "likely_parked"]
# 持续超过这个时长的"拥堵事件"，大概率是车辆停驶/收车停在原地(速度一直趴在阈值下但
# 不是真拥堵)，不是真实的交通拥堵。不删除，只标记likely_parked=1，交给下游分析决定
# 要不要排除——实测2M行样本数据里出现过持续24小时的"拥堵事件"，明显是这种情况。
LIKELY_PARKED_DURATION_SEC = 1800


def flush_run(run, taxi_id, min_duration_sec, hex_size, event_writer, stats):
    """run结束时判断是否满足'持续>min_duration_sec'，满足就写一条拥堵事件。"""
    if len(run) < 2:
        return
    duration = run[-1]["ts"] - run[0]["ts"]
    if duration < min_duration_sec:
        return
    total_speed = sum(r["speed"] for r in run if r["speed"] is not None)
    n_speed = sum(1 for r in run if r["speed"] is not None)
    avg_speed = total_speed / n_speed if n_speed else None
    level = classify_congestion_level(avg_speed)
    mid = run[len(run) // 2]
    q, r, _ = bin_points_to_hex(*lonlat_to_utm([mid["lon"]], [mid["lat"]]), hex_size)
    likely_parked = int(duration > LIKELY_PARKED_DURATION_SEC)
    event_writer.write(",".join(str(x) for x in [
        taxi_id, run[0]["ts"], run[-1]["ts"], duration,
        round(avg_speed, 2) if avg_speed is not None else "", level,
        run[0]["lat"], run[0]["lon"], run[-1]["lat"], run[-1]["lon"],
        int(q[0]), int(r[0]), likely_parked,
    ]) + "\n")
    stats["events_written"] += 1
    for pt in run:
        pt["congested"] = True


def process_taxi_block(rows, taxi_id, args, event_writer, hex_hour_acc, stats):
    run = []
    all_points = []  # 本车所有有效点(有速度值的)，用于格子+小时聚合的分母
    for r in rows:
        if r["trip_break"] or r["bad_coord"]:
            flush_run(run, taxi_id, args.min_duration_sec, args.hex_size, event_writer, stats)
            all_points.extend(run)
            run = []
            continue
        if r["speed"] is not None and r["speed"] < args.speed_threshold:
            run.append(r)
        else:
            flush_run(run, taxi_id, args.min_duration_sec, args.hex_size, event_writer, stats)
            all_points.extend(run)
            run = []
            all_points.append(r)
    flush_run(run, taxi_id, args.min_duration_sec, args.hex_size, event_writer, stats)
    all_points.extend(run)

    if not all_points:
        return
    lons = [p["lon"] for p in all_points]
    lats = [p["lat"] for p in all_points]
    qs, rs, _ = bin_points_to_hex(*lonlat_to_utm(lons, lats), args.hex_size)
    for pt, q, r in zip(all_points, qs, rs):
        hour = hour_of_day(pt["ts"])
        key = (int(q), int(r), hour)
        d = hex_hour_acc.get(key)
        if d is None:
            d = hex_hour_acc[key] = {"total": 0, "congested": 0, "speed_sum": 0.0, "speed_n": 0}
        d["total"] += 1
        if pt.get("congested"):
            d["congested"] += 1
        if pt["speed"] is not None:
            d["speed_sum"] += pt["speed"]
            d["speed_n"] += 1


def main():
    args = parse_args()
    out_dir = ensure_out_dir()
    events_path = args.events_out or os.path.join(out_dir, "congestion_events.csv")
    hex_path = args.hex_out or os.path.join(out_dir, "congestion_by_hex_hour.gpkg")

    stats = {"events_written": 0}
    hex_hour_acc = {}

    t0 = time.time()
    total_in = 0
    block_taxi = None
    block_rows = []

    with open(args.input, "r", encoding="utf-8", buffering=1024 * 1024 * 8) as fin, \
            open(events_path, "w", encoding="utf-8", buffering=1024 * 1024 * 8, newline="\n") as fevents:
        fevents.write(",".join(EVENT_HEADER) + "\n")
        header_skipped = False

        for line in fin:
            if not header_skipped:
                header_skipped = True
                continue
            line = line.rstrip("\r\n")
            if not line:
                continue
            p = line.split(",")
            if len(p) != 17:
                continue
            try:
                speed = float(p[COL_SPEED_GPS]) if p[COL_SPEED_GPS] != "" else None
                row = {
                    "ts": int(p[COL_TS]), "lat": float(p[COL_LAT]), "lon": float(p[COL_LON]),
                    "speed": speed, "trip_break": p[COL_TRIP_BREAK] == "1",
                    "bad_coord": p[COL_BAD_COORD] == "1", "congested": False,
                }
            except ValueError:
                continue
            taxi_id = p[1]

            if block_taxi is not None and taxi_id != block_taxi:
                process_taxi_block(block_rows, block_taxi, args, fevents, hex_hour_acc, stats)
                block_rows = []
            block_taxi = taxi_id
            block_rows.append(row)

            total_in += 1
            if total_in % 2_000_000 == 0:
                print(f"已读入 {total_in:,} 行, 拥堵事件={stats['events_written']:,}, "
                      f"格子x小时组合={len(hex_hour_acc):,}, 耗时 {time.time()-t0:.1f}s")
            if args.limit is not None and total_in >= args.limit:
                break

        if block_rows:
            process_taxi_block(block_rows, block_taxi, args, fevents, hex_hour_acc, stats)

    print(f"拥堵事件明细完成. 事件数={stats['events_written']:,} 耗时={time.time()-t0:.1f}s")
    print("输出:", events_path)

    # 汇总成格子+小时的平均速度+4级拥堵等级
    rows = []
    from hex_utils import hex_size_to_circumradius
    circumradius = hex_size_to_circumradius(args.hex_size)
    for (q, r, hour), d in hex_hour_acc.items():
        mean_speed = (d["speed_sum"] / d["speed_n"]) if d["speed_n"] else None
        rows.append({
            "grid_q": q, "grid_r": r, "hour": hour,
            "point_count": d["total"], "congested_point_count": d["congested"],
            "mean_speed_gps_kmh": mean_speed,
            "congestion_level": classify_congestion_level(mean_speed),
        })

    from hex_utils import axial_to_center, hex_polygon
    import geopandas as gpd
    from common import UTM50N_EPSG
    for row in rows:
        cx, cy = axial_to_center(row["grid_q"], row["grid_r"], circumradius)
        row["geometry"] = hex_polygon(cx, cy, circumradius)
    gdf = gpd.GeoDataFrame(rows, crs=f"EPSG:{UTM50N_EPSG}")
    gdf.to_file(hex_path, driver="GPKG")
    print(f"格子x小时聚合完成. 记录数={len(gdf):,}")
    print("输出(含point_count/congested_point_count/mean_speed_gps_kmh/congestion_level):", hex_path)


if __name__ == "__main__":
    main()
