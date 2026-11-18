"""命令行入口。

用法::

    python -m baseline_engine.cli --input baseline_input.json [--output report.json]

退出码：
* 0：评估完成（即使存在不符合项，结论以报告为准）；
* 2：输入配置错误（未知引用、别名成环、重复快照等）；
* 1：参数或文件读取错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .engine import BaselineConfigError, load_input
from .models import (
    STATUS_COMPLIANT,
    STATUS_NONCOMPLIANT,
    STATUS_NO_SNAPSHOT,
    STATUS_REMEDIATE,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="主机管控端服务低权限基线引擎"
    )
    parser.add_argument(
        "--input", "-i", required=True, help="基线输入 JSON 文件路径"
    )
    parser.add_argument(
        "--output", "-o", help="报告输出路径；缺省输出到标准输出"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with open(args.input, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取输入失败: {exc}", file=sys.stderr)
        return 1

    try:
        report = load_input(raw).evaluate()
    except BaselineConfigError as exc:
        print(f"输入配置错误: {exc}", file=sys.stderr)
        return 2

    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.write("\n")
    else:
        print(payload)

    summary = report.summary
    print(
        "评估完成: 合规 {ok} / 待整改 {rem} / 配置不符合 {nc} / "
        "无快照 {ns} / 跳过 {sk}（共 {total}）".format(
            ok=summary[STATUS_COMPLIANT],
            rem=summary[STATUS_REMEDIATE],
            nc=summary[STATUS_NONCOMPLIANT],
            ns=summary[STATUS_NO_SNAPSHOT],
            sk=summary["skipped"],
            total=summary["total"],
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
