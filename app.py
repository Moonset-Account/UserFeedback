#!/usr/bin/env python3
"""教学接收端：传感器批次格式头预检命令行入口。

在分配任何接收缓冲区之前运行本命令，判断批次文件是否可以安全接收。
本工具只读取长度参数做整数运算，不按声明长度创建大对象。

用法示例::

    python3 app.py --record-count 10 --record-length 32 \\
        --header-length 16 --actual-file-size 336 --resource-budget 4096

退出码：
* 0  预检通过（OK）
* 1  预检拒绝（字段越界 / 无法表示 / 截断 / 尾部多余 / 预算不足）
* 2  命令行参数本身无效
"""

from __future__ import annotations

import argparse
import json
import sys

from header_precheck import (
    BUDGET_EXCEEDED,
    CONTENT_TRUNCATED,
    FIELD_OUT_OF_RANGE,
    OK,
    TOTAL_LENGTH_UNREPRESENTABLE,
    TRAILING_EXCESS,
    precheck_header,
)

_EXIT_OK = 0
_EXIT_REJECTED = 1
_EXIT_USAGE = 2


def _nonneg_int(text: str) -> int:
    """argparse 类型：仅接受十进制非负整数字符串。"""
    try:
        value = int(text, 10)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"必须是非负整数，收到: {text!r}")
    if value < 0:
        raise argparse.ArgumentTypeError(f"必须是非负整数，收到: {text!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="传感器批次格式头预检：分配缓冲区前判断文件是否装得下",
    )
    parser.add_argument(
        "--record-count",
        type=_nonneg_int,
        required=True,
        help="头中声明的记录条数（非负整数，uint32 范围）",
    )
    parser.add_argument(
        "--record-length",
        type=_nonneg_int,
        required=True,
        help="每条记录长度（字节，非负整数，uint32 范围）",
    )
    parser.add_argument(
        "--header-length",
        type=_nonneg_int,
        required=True,
        help="格式头长度（字节，非负整数，uint32 范围）",
    )
    parser.add_argument(
        "--actual-file-size",
        type=_nonneg_int,
        required=True,
        help="实际文件字节数（非负整数）",
    )
    parser.add_argument(
        "--resource-budget",
        type=_nonneg_int,
        required=True,
        help="调用方给定的资源预算（字节，非负整数）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    result = precheck_header(
        record_count=args.record_count,
        record_length=args.record_length,
        header_length=args.header_length,
        actual_file_size=args.actual_file_size,
        resource_budget=args.resource_budget,
    )

    payload = result.to_dict()
    payload["input"] = {
        "record_count": args.record_count,
        "record_length": args.record_length,
        "header_length": args.header_length,
        "actual_file_size": args.actual_file_size,
        "resource_budget": args.resource_budget,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return _EXIT_OK if result.code == OK else _EXIT_REJECTED


if __name__ == "__main__":
    sys.exit(main())
