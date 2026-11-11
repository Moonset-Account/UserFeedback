"""命令行入口：python -m collector.cli <svg路径> [--out result.json]"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import collect_file


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="从公开 SVG 实验图采集可见数据点（不读取隐藏数据）"
    )
    parser.add_argument("svg", help="本地 SVG 文件路径")
    parser.add_argument("--out", help="结果 JSON 输出路径（默认打印到 stdout）")
    args = parser.parse_args(argv)

    result = collect_file(args.svg)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"已写出 {args.out}；状态={result['status']}；"
              f"可恢复观测点={result['summary'].get('recoverable_points', 0)}")
    else:
        print(text)
    return 0 if result["status"] != "error" else 2


if __name__ == "__main__":
    sys.exit(main())
