"""命令行入口：读取 JSON 规格，输出 JSON 审查报告。

退出码：0 = ACCEPTABLE，1 = REJECTED，2 = PENDING。
"""
from __future__ import annotations

import argparse
import json
import sys

from .analysis import ACCEPTABLE, REJECTED, VERDICT_PENDING, Analyzer
from .model import load_spec

_EXIT = {ACCEPTABLE: 0, REJECTED: 1, VERDICT_PENDING: 2}


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="bufreview",
        description="审查记录缓冲区复制到外部诊断接口前的初始化覆盖")
    parser.add_argument("spec", help="JSON 规格文件路径（控制流 + 字段布局 + 指令）")
    args = parser.parse_args(argv)

    with open(args.spec, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    spec = load_spec(data)
    report = Analyzer(spec).run()
    json.dump(report.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return _EXIT[report.verdict]
