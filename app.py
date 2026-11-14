#!/usr/bin/env python3
"""密钥退役清点工具 CLI 入口。

用法：
    python3 app.py INVENTORY_JSON [--output PATH] [--compact]

读取资产清单 JSON，输出按密钥划分的退役清点报告（JSON）。
退出码：0 成功；2 输入文件或清单内容不合法。
"""
import argparse
import json
import sys

from retirement_core import InventoryError, build_report


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="密钥退役清点：按密钥列出仍可产生新引用的入口、"
                    "仍需旧公钥验证的存量与可安全退役的最早时点。",
    )
    parser.add_argument("inventory", help="资产清单 JSON 文件路径")
    parser.add_argument("--output", "-o", help="报告输出路径（默认写到标准输出）")
    parser.add_argument("--compact", action="store_true", help="输出紧凑 JSON（不缩进）")
    args = parser.parse_args(argv)

    try:
        with open(args.inventory, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        print(f"error: 无法读取清单文件: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: 清单不是合法 JSON: {exc}", file=sys.stderr)
        return 2

    try:
        report = build_report(data)
    except InventoryError as exc:
        print(f"error: 清单不合法: {exc}", file=sys.stderr)
        return 2

    text = json.dumps(report, ensure_ascii=False, indent=None if args.compact else 2)
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError as exc:
            print(f"error: 无法写入报告文件: {exc}", file=sys.stderr)
            return 2
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
