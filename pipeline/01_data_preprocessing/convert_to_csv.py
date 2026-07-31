#!/usr/bin/env python3
"""提取出租车轨迹的核心字段，并由 TSV 转换为 CSV。"""

import argparse
import csv
import heapq
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path


# 当前文件的 13 个原始字段。抽样统计显示第二列才是车辆唯一标识，
# 第一列更像公司或数据分组编号。
SOURCE_COLUMN_COUNT = 13

OUTPUT_COLUMNS = [
    "group_id",
    "taxi_id",
    "timestamp",
    "speed",
    "latitude",
    "longitude",
    "direction",
    "positioning_valid",
    "occupied",
]


def sort_key(row: list[str]) -> tuple[str, int]:
    """按车辆 ID 排序，同一车辆内按时间戳排序。"""
    return row[1], int(row[2])


def transform(row: list[str]) -> list[str]:
    status_text = row[12]
    return [
        row[0],                           # 原始第1列：公司/分组编号
        row[1],                           # 原始第2列：车辆唯一 ID
        row[2],                           # Unix 时间戳（秒）
        row[3],                           # 速度（原始单位）
        str(float(row[4]) / 100000),      # WGS84 纬度
        str(float(row[5]) / 100000),      # WGS84 经度
        row[6],                           # 方向角
        str(int("定位有效" in status_text)),
        str(int("重车" in status_text)),
    ]


def write_sorted_chunk(rows: list[list[str]], directory: Path, number: int) -> Path:
    rows.sort(key=sort_key)
    path = directory / f"chunk_{number:06d}.csv"
    with path.open("x", encoding="utf-8", newline="") as dst:
        csv.writer(dst).writerows(rows)
    return path


def convert(
    input_path: Path,
    output_path: Path,
    limit: int | None = None,
    chunk_rows: int = 500_000,
    temp_dir: Path | None = None,
) -> None:
    if input_path.resolve() == output_path.resolve():
        raise ValueError("输出文件不能与输入文件相同")
    if output_path.exists():
        raise FileExistsError(f"输出文件已存在：{output_path}")

    rows_read = 0
    bad_rows = 0
    chunk_paths: list[Path] = []
    rows: list[list[str]] = []
    temp_parent = temp_dir or output_path.parent

    temp_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="taxi_sort_", dir=temp_parent) as work:
        work_path = Path(work)
        with input_path.open("r", encoding="utf-8", newline="") as src:
            reader = csv.reader(src, delimiter="\t")
            for line_number, row in enumerate(reader, start=1):
                if limit is not None and rows_read >= limit:
                    break
                if len(row) != SOURCE_COLUMN_COUNT:
                    bad_rows += 1
                    print(
                        f"跳过第 {line_number} 行：应有 {SOURCE_COLUMN_COUNT} 列，实际 {len(row)} 列",
                        file=sys.stderr,
                    )
                    continue
                try:
                    rows.append(transform(row))
                except ValueError:
                    bad_rows += 1
                    print(f"跳过第 {line_number} 行：数值字段无效", file=sys.stderr)
                    continue
                rows_read += 1
                if len(rows) >= chunk_rows:
                    chunk_paths.append(write_sorted_chunk(rows, work_path, len(chunk_paths)))
                    rows = []

        if rows:
            chunk_paths.append(write_sorted_chunk(rows, work_path, len(chunk_paths)))

        with output_path.open("x", encoding="utf-8-sig", newline="") as dst, ExitStack() as stack:
            writer = csv.writer(dst)
            writer.writerow(OUTPUT_COLUMNS)
            readers = [
                csv.reader(stack.enter_context(path.open("r", encoding="utf-8", newline="")))
                for path in chunk_paths
            ]
            writer.writerows(heapq.merge(*readers, key=sort_key))

    print(f"转换完成：写入 {rows_read:,} 行，跳过 {bad_rows:,} 行")
    print(f"输出文件：{output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="输入的 Tab 分隔 TXT 文件")
    parser.add_argument("output", type=Path, help="输出 CSV 文件（不能已存在）")
    parser.add_argument(
        "--limit", type=int, default=None, help="只转换前 N 条有效记录，用于抽样测试"
    )
    parser.add_argument(
        "--chunk-rows", type=int, default=500_000,
        help="每个内存排序块的行数（默认：500000）",
    )
    parser.add_argument(
        "--temp-dir", type=Path, default=None,
        help="临时排序目录的父目录（默认：输出文件所在目录）",
    )
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须是正整数")
    if args.chunk_rows < 1:
        parser.error("--chunk-rows 必须是正整数")

    try:
        convert(args.input, args.output, args.limit, args.chunk_rows, args.temp_dir)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"错误：{exc}\n")


if __name__ == "__main__":
    main()
