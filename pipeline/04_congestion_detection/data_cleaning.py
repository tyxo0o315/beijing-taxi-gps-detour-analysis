import csv
import sqlite3
import os

# ==================== 配置区（根据实际情况修改）====================
INPUT_CSV  = r'F:\vscode daima\20170301_gps_speed.csv'   # 输入文件
OUTPUT_CSV = '20170301_data.csv'                                        # 输出文件（已排除异常段）
TEMP_DB    = 'temp_filter.db'                                           # 临时数据库

# CSV 分隔符（常见为逗号 ','  或制表符 '\t'）
DELIMITER = ','       # 根据你的文件修改

# 关键列名（必须与 CSV 表头完全一致）
COL_TAXI_ID   = 'taxi_id'
COL_TIMESTAMP = 'timestamp'
COL_SPEED     = 'speed_gps_kmh'      # 新速度字段

# ---------- 时间设置 ----------
# 如果你的时间戳是 Unix 秒（如 1488383715），置为 True
# 如果是日期字符串（如 2025-03-15 08:12:03），置为 False，并填写 TIME_FORMAT
TIME_IS_UNIX = True
TIME_FORMAT  = '%Y-%m-%d %H:%M:%S'   # 仅当 TIME_IS_UNIX = False 时生效

# ---------- 过滤阈值 ----------
ZERO_SPEED_THRESHOLD_KMH = 0         # 速度 == 0 视为零速
MAX_ZERO_DURATION_MIN    = 60        # 连续零速超过此分钟数即丢弃

# ---------- 数据库导入性能 ----------
INSERT_BATCH = 5000                  # 批量插入行数

# ==================== 1. CSV → SQLite 并建立排序索引 ====================
def csv_to_sqlite(csv_path, db_path, table_name='raw_data'):
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f, delimiter=DELIMITER)
        header = next(reader)

        # 检查必要列是否存在
        for col in [COL_TAXI_ID, COL_TIMESTAMP, COL_SPEED]:
            if col not in header:
                raise KeyError(f"列 '{col}' 不存在！实际列名：{header}")

        # 所有列以 TEXT 存入（排序时时间列会被当作字符串，但 Unix 数字排序正确）
        columns_def = ', '.join([f'[{col}] TEXT' for col in header])
        cursor.execute(f'CREATE TABLE {table_name} ({columns_def})')

        batch = []
        row_count = 0
        for row in reader:
            if len(row) != len(header):
                continue
            batch.append(row)
            if len(batch) >= INSERT_BATCH:
                cursor.executemany(
                    f'INSERT INTO {table_name} VALUES ({",".join(["?"]*len(header))})',
                    batch
                )
                row_count += len(batch)
                batch = []
                print(f'已导入 {row_count} 行...', end='\r')
        if batch:
            cursor.executemany(
                f'INSERT INTO {table_name} VALUES ({",".join(["?"]*len(header))})',
                batch
            )
            row_count += len(batch)
        print(f'\n导入完成，共 {row_count} 行数据。')

    print('正在创建索引（可能需要几分钟）...')
    cursor.execute(f'CREATE INDEX idx_sort ON {table_name} ({COL_TAXI_ID}, {COL_TIMESTAMP})')
    conn.commit()
    conn.close()
    return table_name, header

# ==================== 2. 流式过滤长时间零速段 ====================
def parse_time(t_str):
    """根据配置，将时间戳字符串转换为可比较的数值（Unix秒或datetime对象）"""
    if TIME_IS_UNIX:
        return int(float(t_str))          # 兼容小数和整数
    else:
        from datetime import datetime
        return datetime.strptime(t_str, TIME_FORMAT)

def time_diff_minutes(start, end):
    """计算两个时间点之间的分钟差"""
    if TIME_IS_UNIX:
        return (end - start) / 60.0
    else:
        return (end - start).total_seconds() / 60.0

def safe_float(s):
    """安全地转换为浮点数，若失败则返回 None"""
    try:
        return float(s)
    except (ValueError, TypeError):
        return None

def filter_zero_speed_from_db(db_path, table_name, columns, output_csv):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    query = f'SELECT * FROM {table_name} ORDER BY {COL_TAXI_ID}, {COL_TIMESTAMP}'
    cursor.execute(query)

    out_columns = columns
    with open(output_csv, 'w', encoding='utf-8', newline='') as f_out:
        writer = csv.writer(f_out, delimiter=DELIMITER)
        writer.writerow(out_columns)

        current_taxi = None
        buffer_rows = []        # 零速缓存
        buffer_start_time = None

        for row_tuple in cursor:
            row = dict(zip(columns, row_tuple))
            taxi = row[COL_TAXI_ID]
            speed = safe_float(row[COL_SPEED])   # ★ 安全转换
            t = parse_time(row[COL_TIMESTAMP])

            # ---------- 车辆切换 ----------
            if taxi != current_taxi:
                # 处理前车的残余零速段
                if buffer_rows:
                    _flush_buffer(writer, buffer_rows, buffer_start_time, t,
                                  time_diff_minutes, MAX_ZERO_DURATION_MIN)
                current_taxi = taxi
                buffer_rows = []
                buffer_start_time = None

            # ---------- 当前行处理 ----------
            if speed is not None and speed == ZERO_SPEED_THRESHOLD_KMH:
                # 明确为零速，加入缓存
                if not buffer_rows:
                    buffer_start_time = t
                buffer_rows.append(row)
            else:
                # 速度不为零，或者速度缺失 -> 都视为非零速，先结束之前的零速段
                if buffer_rows:
                    _flush_buffer(writer, buffer_rows, buffer_start_time, t,
                                  time_diff_minutes, MAX_ZERO_DURATION_MIN)
                    buffer_rows = []
                    buffer_start_time = None
                # 当前行（非零速 / 速度未知）始终保留
                writer.writerow(row.values())

        # 文件结束，处理残余
        if buffer_rows:
            _flush_buffer(writer, buffer_rows, buffer_start_time, None,
                          time_diff_minutes, MAX_ZERO_DURATION_MIN)

    conn.close()
    print(f'过滤完成，输出文件：{output_csv}')
    
def _flush_buffer(writer, buffer_rows, start_time, end_time, diff_func, max_minutes):
    """根据零速段持续时间决定保留或丢弃"""
    if not buffer_rows:
        return
    # 确定结束时间点
    if end_time is None:                          # 文件末尾
        last_t_str = buffer_rows[-1][COL_TIMESTAMP]
        last_time = parse_time(last_t_str)
    else:
        last_time = end_time

    duration = diff_func(start_time, last_time)
    if duration < max_minutes:
        # 保留该段（短时间停车，正常）
        for row in buffer_rows:
            writer.writerow(row.values())
    # 否则，整段丢弃（不加任何写入）

# ==================== 主程序 ====================
if __name__ == '__main__':
    print('=== 阶段1：数据导入与排序 ===')
    tbl, cols = csv_to_sqlite(INPUT_CSV, TEMP_DB, table_name='taxi')

    print('\n=== 阶段2：过滤长时间零速段 ===')
    filter_zero_speed_from_db(TEMP_DB, tbl, cols, OUTPUT_CSV)

    # 可选：删除临时数据库以释放磁盘空间
    # os.remove(TEMP_DB)
    print('全部完成！临时数据库已保留，如需删除请手动删除 temp_filter.db')