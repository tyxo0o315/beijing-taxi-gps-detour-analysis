r"""
把 20170301_core.csv 转成带派生特征的 20170301_core_features.csv。

跟旧pipeline(TaxiTraj\20170301\heatmaps\01_feature_engineering_v2.py)的关系
========================================================================
v2 存在的原因是修复v1的bug：原始文件按第1列(所谓"taxi_id")物理分块存放，但那一列
其实是公司/车队编号，同一个值下有几百辆不同的车，按它分组会把互不相关车辆的记录
拼成"假轨迹"，速度/航向全错。v2的修法是先用DuckDB把全量数据按真正唯一的
device_id(32位hex)+timestamp做一次核外全局排序，再流式处理。

convert_to_csv.py（生成这份core.csv的脚本）已经在源头把这个问题解决了：
- 它把原始第2列(真正唯一对应车辆的32位hex号)重命名成了这份数据里的 "taxi_id"
  （原始第1列的公司/车队编号被重命名成 "group_id"，不会再被误当成分组键）
- 而且已经用外部归并排序按 (taxi_id, timestamp) 做了全局排序
也就是说"分组键选对+全局排序"这两件事在数据源头就已经做完了，这里不需要再跑一次
DuckDB —— 可以放心退回v1那种"单遍流式、按taxi_id变化切块"的简单写法，同时天然
获得v2的正确性。

数据被砍掉的字段，这一层没法找补
--------------------------------
- 没有旧管道的device_id列(概念还在，只是本来就是这份数据的taxi_id列)
- 没有door_acc/code1/code2/res1/res2，所以door_open / acc_on两列没法算，直接不输出
- status_text被convert_to_csv.py收窄成两个布尔量：
    positioning_valid = "定位有效" in status_text
    occupied          = "重车"     in status_text
  丢失了 空车/驻车/停运/任务车/未知 之间的区分。下面event_type(pickup/dropoff)只能
  按occupied的0/1翻转判定，比旧管道"必须从'空车'转到'重车'"更宽松——比如"停运"->
  "重车"现在也会被计一次pickup，旧管道不会。这是数据本身的信息损失，不是本脚本的bug。

新增的东西
----------
- speed列：设备自报速度(原始单位，大概率km/h)。抽样300万行只有1个非数字脏值("1f5")，
  绝大多数干净，解析失败按缺失处理。旧管道唯一能拿来交叉校验GPS差分速度的res2里程
  字段几乎全是稀疏空值(抽样非零占比≈0)，没法用；这份数据的speed字段能用，
  下面顺带输出 speed_reported_kmh 供交叉校验（新数据相对旧管道的一个改进点）。
- is_outlier_drift：transbigdata twoside漂移判定，公式跟v2完全一致(照抄过来)。

用法:
    python 01_feature_engineering.py --limit 500000   # 只处理前N行，测试用
    python 01_feature_engineering.py                  # 全量
"""
import argparse
import math
import time
import datetime

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CORE_CSV, FEATURES_CSV, haversine_m, circular_heading_diff

MAX_GAP_SEC_DEFAULT = 1800  # 超过这个间隔视为两段独立行程，不跨段算差分/不判事件
PROGRESS_EVERY = 2_000_000
SPEEDLIMIT = 80.0
DISLIMIT = 1000.0
ANGLELIMIT = 30.0

OUT_HEADER = [
    "group_id", "taxi_id", "timestamp", "latitude", "longitude", "direction", "occupied",
    "positioning_valid",
    "dt_sec", "speed_gps_kmh", "speed_reported_kmh", "heading_delta_deg",
    "trip_break", "bad_coord", "out_of_day", "event_type", "is_outlier_drift",
]

CN_LON = (73.0, 135.0)
CN_LAT = (18.0, 53.0)
_CST = datetime.timezone(datetime.timedelta(hours=8))
DEFAULT_DATE = "2017-03-01"  # 数据本身固定是这一天；换年份/换日期跑时用--date覆盖，
                              # 不需要改代码——这也是本pipeline能直接复用到其它年份的原因


def day_range_utc(date_str):
    """'YYYY-MM-DD'(北京时间) -> 当天[00:00, 次日00:00)对应的unix秒范围。"""
    y, m, d = (int(x) for x in date_str.split("-"))
    start = int(datetime.datetime(y, m, d, 0, 0, 0, tzinfo=_CST).timestamp())
    return start, start + 86400


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=CORE_CSV)
    p.add_argument("--output", default=FEATURES_CSV)
    p.add_argument("--limit", type=int, default=None, help="只处理前N行(不含表头)，测试用")
    p.add_argument("--max-gap-sec", type=int, default=MAX_GAP_SEC_DEFAULT)
    p.add_argument("--date", default=DEFAULT_DATE,
                   help="数据对应的日期(YYYY-MM-DD，北京时间)，用于out_of_day校验；"
                        "换年份/日期的数据时改这个参数即可，不用改代码。默认2017-03-01")
    return p.parse_args()


def parse_row(line):
    p = line.split(",")
    if len(p) != 9:
        return None
    try:
        speed_reported = None
        try:
            speed_reported = float(p[3])
        except ValueError:
            pass  # 极少数脏值(比如"1f5")，按缺失处理，不当成坏行丢弃整行
        return {
            "group_id": p[0], "taxi_id": p[1], "ts": int(p[2]),
            "speed_reported": speed_reported,
            "lat": float(p[4]), "lon": float(p[5]), "direction": float(p[6]),
            "pos_valid": p[7] == "1", "occupied": p[8] == "1",
        }
    except ValueError:
        return None


def drift_flags(prev, cur, nxt):
    """精确复刻 transbigdata.traj_clean_drift(method='twoside') 的三条判定规则。"""
    dis_pre = haversine_m(cur["lat"], cur["lon"], prev["lat"], prev["lon"])
    dis_next = haversine_m(cur["lat"], cur["lon"], nxt["lat"], nxt["lon"])
    dis_prenext = haversine_m(prev["lat"], prev["lon"], nxt["lat"], nxt["lon"])

    tg_pre = cur["ts"] - prev["ts"]
    tg_next = nxt["ts"] - cur["ts"]
    tg_prenext = nxt["ts"] - prev["ts"]
    if tg_pre <= 0 or tg_next <= 0 or tg_prenext <= 0:
        return False

    speed_pre = dis_pre / tg_pre * 3.6
    speed_next = dis_next / tg_next * 3.6
    speed_prenext = dis_prenext / tg_prenext * 3.6

    speed_flag = speed_pre > SPEEDLIMIT and speed_next > SPEEDLIMIT and speed_prenext < SPEEDLIMIT
    dist_flag = dis_pre > DISLIMIT and dis_next > DISLIMIT and dis_prenext < DISLIMIT

    angle_flag = False
    if dis_pre > 0 and dis_next > 0:
        cos_a = (dis_pre ** 2 + dis_next ** 2 - dis_prenext ** 2) / (2 * dis_pre * dis_next)
        cos_a = max(-1.0, min(1.0, cos_a))
        angle = math.degrees(math.acos(cos_a))
        angle_flag = angle < ANGLELIMIT

    return speed_flag or dist_flag or angle_flag


def process_block(rows, max_gap_sec, out, stats, day_start_utc, day_end_utc):
    # 数据已经全局按(taxi_id, timestamp)排序，这里的sort只是保险(同taxi_id内本就有序)
    rows.sort(key=lambda r: r["ts"])
    dedup = []
    last_ts = None
    for r in rows:
        if r["ts"] == last_ts:
            stats["dup_ts_dropped"] += 1
            continue
        dedup.append(r)
        last_ts = r["ts"]

    n = len(dedup)
    prev_occupied = None
    for i in range(n):
        cur = dedup[i]
        prev = dedup[i - 1] if i > 0 else None
        nxt = dedup[i + 1] if i < n - 1 else None

        bad_coord = not (CN_LON[0] <= cur["lon"] <= CN_LON[1] and CN_LAT[0] <= cur["lat"] <= CN_LAT[1])
        out_of_day = not (day_start_utc <= cur["ts"] < day_end_utc)
        if bad_coord:
            stats["bad_coord"] += 1
        if out_of_day:
            stats["out_of_day"] += 1

        dt_sec = speed_gps = heading_delta = ""
        trip_break = True
        event_type = ""

        if prev is not None:
            dt = cur["ts"] - prev["ts"]
            if 0 < dt <= max_gap_sec:
                trip_break = False
                dist_m = haversine_m(prev["lat"], prev["lon"], cur["lat"], cur["lon"])
                speed_gps = round(dist_m / dt * 3.6, 2)
                if speed_gps > 200:
                    stats["speed_gps_over_200"] += 1
                heading_delta = round(circular_heading_diff(prev["direction"], cur["direction"]), 1)
                dt_sec = dt

                if prev_occupied is False and cur["occupied"] is True:
                    event_type = "pickup"
                elif prev_occupied is True and cur["occupied"] is False:
                    event_type = "dropoff"
            else:
                stats["trip_breaks"] += 1

        is_outlier = 0
        if prev is not None and nxt is not None:
            if drift_flags(prev, cur, nxt):
                is_outlier = 1
                stats["outlier_drift"] += 1

        speed_reported_kmh = "" if cur["speed_reported"] is None else cur["speed_reported"]
        if cur["speed_reported"] is None:
            stats["speed_reported_bad"] += 1

        out.write(",".join(str(x) for x in [
            cur["group_id"], cur["taxi_id"], cur["ts"], cur["lat"], cur["lon"], cur["direction"],
            int(cur["occupied"]), int(cur["pos_valid"]),
            dt_sec, speed_gps, speed_reported_kmh, heading_delta,
            int(trip_break), int(bad_coord), int(out_of_day), event_type, is_outlier,
        ]) + "\n")

        prev_occupied = cur["occupied"]
        stats["rows_written"] += 1


def main():
    args = parse_args()
    day_start_utc, day_end_utc = day_range_utc(args.date)
    stats = {"rows_written": 0, "dup_ts_dropped": 0, "trip_breaks": 0,
              "bad_coord": 0, "out_of_day": 0, "speed_gps_over_200": 0,
              "bad_lines": 0, "outlier_drift": 0, "speed_reported_bad": 0}

    start = time.time()
    total_in = 0
    block_taxi = None
    block_rows = []

    with open(args.input, "r", encoding="utf-8-sig", buffering=1024 * 1024 * 8) as fin, \
            open(args.output, "w", encoding="utf-8", buffering=1024 * 1024 * 8, newline="\n") as fout:

        fout.write(",".join(OUT_HEADER) + "\n")
        header_skipped = False

        for line in fin:
            if not header_skipped:
                header_skipped = True
                continue
            line = line.rstrip("\r\n")
            if not line:
                continue
            row = parse_row(line)
            if row is None:
                stats["bad_lines"] += 1
                continue

            if block_taxi is not None and row["taxi_id"] != block_taxi:
                process_block(block_rows, args.max_gap_sec, fout, stats, day_start_utc, day_end_utc)
                block_rows = []
            block_taxi = row["taxi_id"]
            block_rows.append(row)

            total_in += 1
            if total_in % PROGRESS_EVERY == 0:
                print(f"已读入 {total_in:,} 行, 已写出 {stats['rows_written']:,} 行, "
                      f"耗时 {time.time()-start:.1f}s")

            if args.limit is not None and total_in >= args.limit:
                break

        if block_rows:
            process_block(block_rows, args.max_gap_sec, fout, stats, day_start_utc, day_end_utc)

    elapsed = time.time() - start
    print(f"完成. 读入={total_in:,} 写出={stats['rows_written']:,} 耗时={elapsed:.1f}s")
    print(f"坏行(字段数不对/解析失败)={stats['bad_lines']:,}")
    print(f"同taxi_id重复timestamp丢弃={stats['dup_ts_dropped']:,}")
    print(f"行程中断(间隔>{args.max_gap_sec}s，不跨段算差分/事件)={stats['trip_breaks']:,}")
    print(f"坐标越界(标记但未丢弃)={stats['bad_coord']:,}")
    print(f"非{args.date}当天(标记但未丢弃)={stats['out_of_day']:,}")
    print(f"GPS差分速度>200km/h(标记但未丢弃)={stats['speed_gps_over_200']:,} "
          f"({stats['speed_gps_over_200']/max(stats['rows_written'],1)*100:.3f}%)")
    print(f"is_outlier_drift标记数={stats['outlier_drift']:,} "
          f"({stats['outlier_drift']/max(stats['rows_written'],1)*100:.3f}%)")
    print(f"speed字段解析失败(脏值，按缺失处理)={stats['speed_reported_bad']:,}")
    print(f"输出文件: {args.output}")


if __name__ == "__main__":
    main()
