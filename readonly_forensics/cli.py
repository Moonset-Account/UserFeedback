"""命令行入口：读取本地案例 JSON，输出只读取证恢复报告。

用法::

    python -m readonly_forensics.cli case.json
    python -m readonly_forensics.cli case.json --output report.json
    python -m readonly_forensics.cli case.json --format text

程序只读取给定案例文件并向标准输出（或指定新文件）写报告，
不触碰任何原始证据路径。
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import recover_file
from .model import RecoveryCase


def _load_case(path: str) -> RecoveryCase:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return RecoveryCase.from_dict(payload)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m readonly_forensics.cli",
        description="只读删除文件取证恢复：按归属与采集证据逐区段判定",
    )
    parser.add_argument("case_file", help="案例 JSON 文件路径")
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="报告格式，默认 json",
    )
    parser.add_argument(
        "--output",
        help="报告输出路径；缺省输出到标准输出（不会写回证据文件）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    case = _load_case(args.case_file)
    report = recover_file(case)

    if args.format == "json":
        rendered = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    else:
        rendered = report.render_text()

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.write("\n")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
