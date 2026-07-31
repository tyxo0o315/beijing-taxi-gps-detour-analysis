r"""
一条命令跑完整条流水线：01转点 -> 02按研究区筛选车辆 -> 03提取起终点，对--days里的每一天
依次执行，每天产出独立的 <day>_trips.csv / <day>_incomplete_trips.csv。

用法(最简单，用所有默认列名，只需要给两个路径):
    python run_all.py --base-dir <7天CSV所在目录> --study-area-shp <研究区面路径>

如果你的CSV列名跟默认值不一样，把对应的 --xxx-col 参数加在后面即可，run_all.py会把它们
转发给01/02/03这三步。具体每个参数的含义见对应脚本的 --help，或者直接看 README.md。
"""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DAYS = [f"201703{d:02d}" for d in range(1, 8)]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-dir", required=True, help="7天CSV所在的根目录，结构 <base-dir>\\<day>_data\\<day>_data.csv")
    p.add_argument("--study-area-shp", required=True)
    p.add_argument("--days", nargs="+", default=DEFAULT_DAYS)
    p.add_argument("--chunk-size", type=int, default=2_000_000)
    p.add_argument("--id-col", default="taxi_id")
    p.add_argument("--time-col", default="timestamp")
    p.add_argument("--lat-col", default="latitude")
    p.add_argument("--lon-col", default="longitude")
    p.add_argument("--congestion-col", default="congestion")
    p.add_argument("--pickup-col", default="occupied")
    p.add_argument("--bad-coord-col", default="bad_coord")
    p.add_argument("--occupied-value", default="1")
    p.add_argument("--skip-build-points", action="store_true",
                    help="跳过第1步(转点，只是给你留一份点矢量文件方便可视化，不影响2/3步结果)，"
                         "想快点跑完起终点结果的话可以加这个")
    p.add_argument("--trip-points-output-name", default=None,
                    help="可选：每天额外产出一份逐点轨迹矢量文件(文件名，比如 trip_points.gpkg，"
                         "会自动落到对应<day>_data文件夹下)，包含每趟行程起点到终点之间的全部"
                         "中间GPS点，带trip_id/seq，用于以后连线或逐点分析。默认不产出。")
    p.add_argument("--trip-lines-output-name", default=None,
                    help="可选：每天额外产出一份行程折线矢量文件(文件名，比如 trip_lines.gpkg)，"
                         "每个trip_id一条LineString，已经按时间顺序连好线，可直接用。默认不产出。")
    return p.parse_args()


def run(cmd):
    print("+", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    subprocess.run(cmd, check=True)


def main():
    args = parse_args()
    py = sys.executable

    for day in args.days:
        t0 = time.time()
        print(f"\n===== {day} =====")

        if not args.skip_build_points:
            run([py, os.path.join(HERE, "01_build_points.py"),
                 "--base-dir", args.base_dir, "--days", day, "--chunk-size", str(args.chunk_size),
                 "--id-col", args.id_col, "--time-col", args.time_col,
                 "--lat-col", args.lat_col, "--lon-col", args.lon_col,
                 "--congestion-col", args.congestion_col, "--pickup-col", args.pickup_col,
                 "--bad-coord-col", args.bad_coord_col])

        run([py, os.path.join(HERE, "02_filter_by_study_area.py"),
             "--base-dir", args.base_dir, "--days", day, "--study-area-shp", args.study_area_shp,
             "--chunk-size", str(args.chunk_size), "--id-col", args.id_col,
             "--lat-col", args.lat_col, "--lon-col", args.lon_col,
             "--pickup-col", args.pickup_col, "--occupied-value", args.occupied_value,
             "--bad-coord-col", args.bad_coord_col])

        day_dir = os.path.join(args.base_dir, f"{day}_data")
        filtered_csv = os.path.join(day_dir, f"{day}_filtered.csv")
        trips_out = os.path.join(day_dir, f"{day}_trips.csv")
        incomplete_out = os.path.join(day_dir, f"{day}_incomplete_trips.csv")

        step3_cmd = [py, os.path.join(HERE, "03_extract_od_trips.py"),
                     "--input", filtered_csv, "--output", trips_out, "--incomplete-output", incomplete_out,
                     "--id-col", args.id_col, "--time-col", args.time_col,
                     "--lon-col", args.lon_col, "--lat-col", args.lat_col,
                     "--congestion-col", args.congestion_col, "--pickup-col", args.pickup_col,
                     "--occupied-value", args.occupied_value]
        if args.trip_points_output_name:
            step3_cmd += ["--trip-points-output", os.path.join(day_dir, f"{day}_{args.trip_points_output_name}")]
        if args.trip_lines_output_name:
            step3_cmd += ["--trip-lines-output", os.path.join(day_dir, f"{day}_{args.trip_lines_output_name}")]
        run(step3_cmd)

        print(f"[{day}] 全部完成，耗时 {time.time()-t0:.1f}s -> {trips_out}")

    print("\n全部7天完成.")


if __name__ == "__main__":
    main()
