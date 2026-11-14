#!/usr/bin/env python3
"""电子证物交接事件台账重建 —— CLI 主入口。

用法:
  python3 app.py --input a.jsonl b.jsonl [--input-dir DIR] [--output OUT] [--pretty]

输入:JSONL 事件文件,可分批、乱序导出。
输出:每台证物的当前保管状态、待确认移交、需补查的原始记录位置(JSON)。
"""
import argparse
import json
import os
import sys

from ledger_core import build_report, load_records


def collect_paths(inputs, input_dirs):
    paths = list(inputs)
    for d in input_dirs:
        if not os.path.isdir(d):
            raise SystemExit("错误: 输入目录不存在: %s" % d)
        for name in sorted(os.listdir(d)):
            if name.endswith(".jsonl"):
                paths.append(os.path.join(d, name))
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="电子证物交接事件台账重建(入库/移交发起/接收确认/撤销/复核)")
    parser.add_argument("--input", nargs="*", default=[],
                        help="一个或多个 JSONL 事件文件")
    parser.add_argument("--input-dir", action="append", default=[],
                        help="包含 .jsonl 事件文件的目录(可多次指定)")
    parser.add_argument("--output",
                        help="报告输出路径(缺省写到标准输出)")
    parser.add_argument("--pretty", action="store_true",
                        help="缩进排版输出 JSON")
    args = parser.parse_args(argv)

    paths = collect_paths(args.input, args.input_dir)
    if not paths:
        parser.error("至少需要一个 --input 文件或 --input-dir 目录")

    try:
        events, invalid = load_records(paths)
    except OSError as exc:
        print("错误: 读取输入失败: %s" % exc, file=sys.stderr)
        return 1

    report = build_report(events, invalid)
    text = json.dumps(report, ensure_ascii=False, sort_keys=True,
                      indent=2 if args.pretty else None)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print("已写出报告: %s (证物 %d 件, 无法入档记录 %d 条)"
              % (args.output, report["evidence_count"],
                 len(report["invalid_records"])), file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
