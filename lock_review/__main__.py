"""命令行入口：``python -m lock_review <spec.json>``。

读取维护者导出的规范文件，输出 JSON 格式的审查报告；
发现可能阻断处置的锁等待环时退出码为 1，否则为 0。
"""

from __future__ import annotations

import argparse
import json
import sys

from .analyzer import review_file


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="lock_review",
        description="审查抽象动作流中可能阻断处置的锁等待环",
    )
    parser.add_argument("spec", help="维护者导出的规范 JSON 文件路径")
    args = parser.parse_args(argv)

    report = review_file(args.spec)
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
