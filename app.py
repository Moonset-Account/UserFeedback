#!/usr/bin/env python3
"""合规核对工具 CLI 入口：重登记设备的卷加密与当前密钥托管核验。

用法：
    python3 app.py --inventory inventory.json --reports reports.json \
        --receipts receipts.json --check-time 2026-11-14T12:00:00Z [--output out.json]

退出码：0 = 核对成功执行（不代表全部设备通过）；2 = 输入错误。
"""
from __future__ import annotations

import argparse
import json
import sys

from compliance_core import evaluate, parse_time


def _load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _extract_records(doc, key):
    """接受 {"<key>": [...]} 或裸数组两种形式。"""
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        records = doc.get(key, [])
        if not isinstance(records, list):
            raise ValueError(f"字段 {key} 必须是数组")
        return records
    raise ValueError("输入 JSON 必须是对象或数组")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="重登记设备的卷加密与当前密钥托管合规核对"
    )
    parser.add_argument("--inventory", required=True, help="资产清单 JSON")
    parser.add_argument("--reports", required=True, help="终端加密上报 JSON")
    parser.add_argument("--receipts", required=True, help="恢复密钥托管回执 JSON")
    parser.add_argument("--check-time", required=True, help="本次核对时间（ISO 8601）")
    parser.add_argument("--output", help="报告输出路径（缺省打印到 stdout）")
    args = parser.parse_args(argv)

    try:
        inventory = _load_json(args.inventory)
        reports = _extract_records(_load_json(args.reports), "reports")
        receipts = _extract_records(_load_json(args.receipts), "receipts")
        check_time = parse_time(args.check_time, "check_time")
        report = evaluate(inventory, reports, receipts, check_time)
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f"输入错误: {exc}", file=sys.stderr)
        return 2

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
