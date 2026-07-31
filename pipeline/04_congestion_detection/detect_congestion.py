import csv
import os
import shutil
import math

# ==================== 配置 ====================
INPUT_CSV  = '20170301_data.csv'          # 你的干净轨迹文件（将被直接覆盖）
# 以下列名必须与 CSV 表头一致
COL_TAXI_ID   = 'taxi_id'
COL_TIMESTAMP = 'timestamp'               # Unix 秒（整数）
COL_SPEED     = 'speed_gps_kmh'

# 拥堵判定参数
SPEED_THRESHOLD_KMH = 20                # 速度低于20视为低速
MIN_DURATION_SEC    = 4 * 60              # 持续至少 240 秒（4 分钟）才算拥堵

# 临时输出文件名（处理完成后替换原文件）
TEMP_OUTPUT = INPUT_CSV + '.tmp_congestion'

# ==================== 主处理函数 ====================
def add_congestion_label_streaming():
    """
    逐行读取原始 CSV，为每条记录添加 congestion 列（0/1），
    输出到临时文件，最后原子替换原文件。
    """
    if not os.path.exists(INPUT_CSV):
        raise FileNotFoundError(f"找不到文件: {INPUT_CSV}")

    with open(INPUT_CSV, 'r', encoding='utf-8') as fin, \
         open(TEMP_OUTPUT, 'w', encoding='utf-8', newline='') as fout:

        reader = csv.DictReader(fin)
        # 原始列名 + 新增 congestion
        fieldnames = reader.fieldnames + ['congestion']
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()

        # 状态变量
        current_taxi = None
        low_speed_buffer = []      # 存储当前低速段的全部行
        seg_start_ts = None        # 段首时间戳（秒）
        seg_end_ts = None          # 段尾时间戳（秒）

        def flush_buffer(force_congestion=False):
            """
            输出缓存区中的所有行，并根据 duration 判断是否标记为拥堵。
            若 force_congestion 为 True，则一律标记 1（用于段结束时满足条件的情况）。
            """
            nonlocal current_taxi

            if not low_speed_buffer:
                return

            # 计算段时长
            duration = (seg_end_ts - seg_start_ts) if (seg_start_ts is not None and seg_end_ts is not None) else 0
            is_congested = (duration >= MIN_DURATION_SEC)

            for row in low_speed_buffer:
                row['congestion'] = '1' if (is_congested or force_congestion) else '0'
                writer.writerow(row)

            # 清空缓存
            low_speed_buffer.clear()

        def append_to_buffer(row, ts):
            """将一行加入低速段缓存，并更新时间戳范围。"""
            nonlocal seg_start_ts, seg_end_ts
            if not low_speed_buffer:
                seg_start_ts = ts
            low_speed_buffer.append(row)
            seg_end_ts = ts

        for row in reader:
            # 解析关键字段
            taxi = row[COL_TAXI_ID]
            try:
                ts = int(row[COL_TIMESTAMP])
            except (ValueError, KeyError):
                # 时间戳错误，跳过该行（你也可以标记为0直接输出，这里选择跳过）
                continue

            # 解析速度（空值 → NaN，视为非低速）
            speed_raw = row.get(COL_SPEED, '')
            try:
                speed = float(speed_raw) if speed_raw.strip() != '' else float('nan')
            except ValueError:
                speed = float('nan')

            # 车辆切换：输出前一辆车的缓存段，并重置计数器
            if taxi != current_taxi:
                if current_taxi is not None:
                    flush_buffer()
                current_taxi = taxi
                low_speed_buffer.clear()
                seg_start_ts = seg_end_ts = None

            # 判断当前点是否属于低速
            is_low = (not math.isnan(speed)) and speed < SPEED_THRESHOLD_KMH

            if is_low:
                # 加入低速缓存
                append_to_buffer(row, ts)
            else:
                # 遇到非低速点，意味着上一个低速段（如果有）结束
                if low_speed_buffer:
                    flush_buffer()
                # 当前行直接输出，标记为0
                row['congestion'] = '0'
                writer.writerow(row)

        # 文件末尾，处理最后一辆车的最后一个段
        if low_speed_buffer:
            flush_buffer()

    # 原子替换原文件（不会产生额外备份）
    os.replace(TEMP_OUTPUT, INPUT_CSV)
    print(f"处理完成，原文件 {INPUT_CSV} 已更新（最后一列为 congestion）。")

if __name__ == '__main__':
    add_congestion_label_streaming()