r"""
把01阶段产出的features.csv里零散的pickup/dropoff事件点，配对成完整的"行程"记录
(trips.csv)和"空驶找客"记录(idle_segments.csv)，是后面OD矩阵/距离时长分布/流向图/
车辆利用率/供需匹配效率这几个分析共同的基础输入。

方法
----
features.csv已经按(taxi_id, timestamp)全局排序好了(继承自01阶段的输入)，这里按
taxi_id分块、块内按时间顺序单遍扫描，维护一个简单的状态机：
    看到event_type='pickup' -> 结束当前的"空驶找客"阶段(如果有)，开始一段新行程
    看到event_type='dropoff' -> 结束当前的行程(如果有)，开始一段新的"空驶找客"阶段
行程/空驶阶段的距离，用阶段内相邻两点的haversine距离累加得到(沿实际GPS轨迹算，
不是起点到终点的直线距离——两者都会输出，直线距离供快速对比用)。

数据质量兜底：阶段进行中如果遇到trip_break=1(间隔>1800s)或bad_coord=1(坐标越界)，
视为这一段数据不可信，直接丢弃这一整段正在累积的行程/空驶记录(不强行拼接)，避免
GPS丢包/坐标异常污染距离统计。这跟01阶段"只标记不删除"的原则不冲突——01的输出
文件本身完整保留，这里只是在"配对成行程"这一步选择性地跳过不可信的片段。

用法:
    python 13_od_trip_extraction.py
    python 13_od_trip_extraction.py --hex-size 500   # 跟09/12脚本的--hex-size同一语义
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import FEATURES_CSV, OUT_DIR, ensure_out_dir, haversine_m
from kde_utils import lonlat_to_utm
from hex_utils import bin_points_to_hex

TRIP_HEADER = [
    "taxi_id", "group_id", "pickup_ts", "pickup_lat", "pickup_lon",
    "dropoff_ts", "dropoff_lat", "dropoff_lon",
    "duration_sec", "path_distance_m", "straight_distance_m", "avg_speed_kmh",
    "pickup_hex_q", "pickup_hex_r", "dropoff_hex_q", "dropoff_hex_r", "is_plausible",
]
# 合理性标记的阈值：时长<30秒、>2小时，或平均速度>120km/h，大概率是event_type
# 误判(比如"停运->重车"被误判成pickup)导致的伪行程，不是真实载客行程。不删除这些
# 记录(万一有人要专门研究这类异常)，只标记is_plausible=False，交给下游分析决定
# 要不要过滤——跟01阶段"只标记不删除"是同一个原则。
MIN_PLAUSIBLE_DURATION_SEC = 30
MAX_PLAUSIBLE_DURATION_SEC = 7200
MAX_PLAUSIBLE_SPEED_KMH = 120.0
IDLE_HEADER = [
    "taxi_id", "group_id", "empty_start_ts", "empty_start_lat", "empty_start_lon",
    "next_pickup_ts", "next_pickup_lat", "next_pickup_lon",
    "search_duration_sec", "search_distance_m",
    "empty_hex_q", "empty_hex_r",
]

# features.csv列顺序(01_feature_engineering.py的OUT_HEADER)，按位置读取，不解析表头。
COL_GROUP_ID, COL_TAXI_ID, COL_TS, COL_LAT, COL_LON = 0, 1, 2, 3, 4
COL_OCCUPIED, COL_TRIP_BREAK, COL_BAD_COORD, COL_EVENT_TYPE = 6, 12, 13, 15


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=FEATURES_CSV)
    p.add_argument("--trips-out", default=None)
    p.add_argument("--idle-out", default=None)
    p.add_argument("--hex-size", type=float, default=300.0)
    p.add_argument("--limit", type=int, default=None, help="只处理前N行，测试用")
    return p.parse_args()


class TripState:
    """一辆车当前正在累积的"行程"或"空驶"阶段。phase取值: None/'trip'/'idle'。"""

    def __init__(self):
        self.phase = None
        self.start_ts = None
        self.start_lat = None
        self.start_lon = None
        self.cum_dist = 0.0
        self.prev_lat = None
        self.prev_lon = None

    def reset(self):
        self.__init__()

    def start(self, phase, ts, lat, lon):
        self.phase = phase
        self.start_ts = ts
        self.start_lat = lat
        self.start_lon = lon
        self.cum_dist = 0.0
        self.prev_lat = lat
        self.prev_lon = lon

    def accumulate(self, lat, lon):
        self.cum_dist += haversine_m(self.prev_lat, self.prev_lon, lat, lon)
        self.prev_lat, self.prev_lon = lat, lon


def process_taxi_block(rows, taxi_id, group_id, hex_size, trip_writer, idle_writer, stats):
    state = TripState()
    for i, r in enumerate(rows):
        ts, lat, lon = r["ts"], r["lat"], r["lon"]
        if r["trip_break"] or r["bad_coord"]:
            # 数据质量兜底: 丢弃当前正在累积的这一段(不强行跨断点拼接)
            if state.phase is not None:
                stats[f"{state.phase}_discarded_bad_data"] += 1
            state.reset()
            continue

        if state.phase is None:
            if r["event_type"] == "pickup":
                state.start("trip", ts, lat, lon)
            elif r["occupied"] == 0:
                state.start("idle", ts, lat, lon)
            continue

        state.accumulate(lat, lon)

        if r["event_type"] == "pickup" and state.phase == "idle":
            straight = haversine_m(state.start_lat, state.start_lon, lat, lon)
            idle_writer.write(",".join(str(x) for x in [
                taxi_id, group_id, state.start_ts, state.start_lat, state.start_lon,
                ts, lat, lon, ts - state.start_ts, round(state.cum_dist, 1),
            ]) + "\n")
            stats["idle_written"] += 1
            state.start("trip", ts, lat, lon)
        elif r["event_type"] == "dropoff" and state.phase == "trip":
            straight = haversine_m(state.start_lat, state.start_lon, lat, lon)
            duration = ts - state.start_ts
            avg_speed = round(state.cum_dist / duration * 3.6, 2) if duration > 0 else ""
            pq, pr, circumradius = bin_points_to_hex(*lonlat_to_utm([state.start_lon], [state.start_lat]), hex_size)
            dq, dr, _ = bin_points_to_hex(*lonlat_to_utm([lon], [lat]), hex_size)
            is_plausible = (
                MIN_PLAUSIBLE_DURATION_SEC <= duration <= MAX_PLAUSIBLE_DURATION_SEC
                and (avg_speed == "" or avg_speed <= MAX_PLAUSIBLE_SPEED_KMH)
            )
            trip_writer.write(",".join(str(x) for x in [
                taxi_id, group_id, state.start_ts, state.start_lat, state.start_lon,
                ts, lat, lon, duration, round(state.cum_dist, 1), round(straight, 1), avg_speed,
                int(pq[0]), int(pr[0]), int(dq[0]), int(dr[0]), int(is_plausible),
            ]) + "\n")
            stats["trips_written"] += 1
            state.start("idle", ts, lat, lon)
        # 其它情况(比如trip阶段里又出现pickup)按数据本身的状态转移来，不强行纠正，
        # 保持跟01阶段occupied 0/1翻转判定event_type的逻辑一致。


def main():
    args = parse_args()
    out_dir = ensure_out_dir()
    trips_path = args.trips_out or os.path.join(out_dir, "trips.csv")
    idle_path = args.idle_out or os.path.join(out_dir, "idle_segments.csv")

    stats = {"trips_written": 0, "idle_written": 0,
              "trip_discarded_bad_data": 0, "idle_discarded_bad_data": 0}

    t0 = time.time()
    total_in = 0
    block_taxi = None
    block_group = None
    block_rows = []

    with open(args.input, "r", encoding="utf-8", buffering=1024 * 1024 * 8) as fin, \
            open(trips_path, "w", encoding="utf-8", buffering=1024 * 1024 * 8, newline="\n") as ftrips, \
            open(idle_path, "w", encoding="utf-8", buffering=1024 * 1024 * 8, newline="\n") as fidle:

        ftrips.write(",".join(TRIP_HEADER) + "\n")
        fidle.write(",".join(IDLE_HEADER) + "\n")
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
                row = {
                    "ts": int(p[COL_TS]), "lat": float(p[COL_LAT]), "lon": float(p[COL_LON]),
                    "occupied": int(p[COL_OCCUPIED]), "trip_break": p[COL_TRIP_BREAK] == "1",
                    "bad_coord": p[COL_BAD_COORD] == "1", "event_type": p[COL_EVENT_TYPE],
                }
            except ValueError:
                continue
            taxi_id = p[COL_TAXI_ID]

            if block_taxi is not None and taxi_id != block_taxi:
                process_taxi_block(block_rows, block_taxi, block_group, args.hex_size, ftrips, fidle, stats)
                block_rows = []
            block_taxi = taxi_id
            block_group = p[COL_GROUP_ID]
            block_rows.append(row)

            total_in += 1
            if total_in % 2_000_000 == 0:
                print(f"已读入 {total_in:,} 行, 已生成行程={stats['trips_written']:,}, "
                      f"空驶段={stats['idle_written']:,}, 耗时 {time.time()-t0:.1f}s")
            if args.limit is not None and total_in >= args.limit:
                break

        if block_rows:
            process_taxi_block(block_rows, block_taxi, block_group, args.hex_size, ftrips, fidle, stats)

    elapsed = time.time() - t0
    print(f"完成. 读入={total_in:,} 耗时={elapsed:.1f}s")
    print(f"行程数(trips)={stats['trips_written']:,}")
    print(f"空驶找客段数(idle_segments)={stats['idle_written']:,}")
    print(f"因trip_break/bad_coord丢弃的行程片段={stats['trip_discarded_bad_data']:,}")
    print(f"因trip_break/bad_coord丢弃的空驶片段={stats['idle_discarded_bad_data']:,}")
    print(f"输出: {trips_path}")
    print(f"输出: {idle_path}")


if __name__ == "__main__":
    main()
