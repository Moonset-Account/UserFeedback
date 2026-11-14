#!/usr/bin/env python3
"""SEC-VULN-004 受影响依赖符号可达性排查 CLI 主入口。

用法:
    python3 app.py [--data 数据目录] [--mode 模式]... [--out 报告路径] [--json]

退出码: 0 分析成功; 2 输入缺失或校验失败。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reachability.analyzer import analyze
from reachability.loader import load_dataset
from reachability.model import InputError
from reachability.report import render_conclusions

DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def build_parser():
    parser = argparse.ArgumentParser(
        description="SEC-VULN-004: 按运行模式判定受影响依赖符号的可达性")
    parser.add_argument("--data", default=DEFAULT_DATA_DIR,
                        help="输入目录, 需含 lockfile.json / call_edges.json / "
                             "entries.json / affected.json (默认: 随附 data/)")
    parser.add_argument("--mode", action="append", default=None,
                        help="只分析指定运行模式, 可重复; 缺省分析全部模式")
    parser.add_argument("--out", default=None,
                        help="把 JSON 报告写入该路径")
    parser.add_argument("--json", action="store_true",
                        help="同时把 JSON 报告打印到标准输出")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        dataset = load_dataset(args.data)
        report = analyze(dataset, modes=args.mode)
    except InputError as exc:
        print("输入错误: {0}".format(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print("读取输入失败: {0}".format(exc), file=sys.stderr)
        return 2

    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    if args.json:
        print(text)
    print(render_conclusions(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
