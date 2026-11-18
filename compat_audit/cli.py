"""命令行入口。

用法::

    python -m compat_audit <config.json> [--report report.json]

退出码：
* 0 —— 全部组合兼容（或仅有 no_overlap）；
* 2 —— 存在 blocked 组合（阻断性策略冲突）；
* 1 —— 配置文件/结构错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .audit import audit_config
from .config import ConfigError, load_config
from .model import PairStatus


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m compat_audit",
        description="反序列化字段策略的生产者/消费者兼容核查",
    )
    parser.add_argument("config", help="核查配置 JSON 文件路径")
    parser.add_argument(
        "--report",
        help="将完整 JSON 报告写入该路径；缺省时输出到标准输出",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        report = audit_config(config)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"无法读取配置文件: {exc}", file=sys.stderr)
        return 1

    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.write("\n")
    else:
        print(payload)

    for conclusion in report.conclusions:
        marker = {
            PairStatus.COMPATIBLE.value: "OK ",
            PairStatus.BLOCKED.value: "BLK",
            PairStatus.NO_OVERLAP.value: "N/A",
        }[conclusion.status]
        print(
            f"[{marker}] {conclusion.pair_id} "
            f"status={conclusion.status} "
            f"overlap={','.join(conclusion.overlap_versions) or '-'} "
            f"findings={len(conclusion.findings)}",
            file=sys.stderr,
        )

    return 2 if report.has_blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
