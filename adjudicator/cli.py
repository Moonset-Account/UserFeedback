"""命令行入口。

用法：
  python3 -I run_adjudicator.py \
      --boms data/boms.json \
      --advisories data/advisories-current.json \
      --records data/applicability-records.json \
      [--baseline-advisories data/advisories-baseline.json] \
      [--json-out report.json] [--text-out report.txt]

全程离线，仅使用标准库；-I 隔离模式运行，避免当前目录中的
同名模块（如植入的 json.py）干扰输入解析。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from .engine import Adjudicator
from .loader import load_advisories, load_boms, load_records
from .report import build_report, render_json, render_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="离线漏洞适用性裁决器：比较组件清单、漏洞公告与适用性记录"
    )
    parser.add_argument("--boms", required=True, help="CycloneDX 风格清单 JSON")
    parser.add_argument("--advisories", required=True,
                        help="当前（更新后）漏洞公告 JSON")
    parser.add_argument("--records", required=True, help="适用性记录 JSON")
    parser.add_argument("--baseline-advisories", default=None,
                        help="更新前的公告 JSON；提供后输出版本间变化报告")
    parser.add_argument("--json-out", default=None, help="JSON 报告输出路径")
    parser.add_argument("--text-out", default=None, help="文本报告输出路径")
    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    boms = load_boms(args.boms)
    current_advisories = load_advisories(args.advisories)
    records = load_records(args.records)

    engine = Adjudicator(boms, current_advisories, records)
    current_findings = engine.adjudicate_all()

    changes = None
    baseline_findings = None
    baseline_advisories = None
    if args.baseline_advisories:
        baseline_advisories = load_advisories(args.baseline_advisories)
        baseline_engine = Adjudicator(boms, baseline_advisories, records)
        baseline_findings = baseline_engine.adjudicate_all()
        changes = engine.diff(
            baseline_findings, current_findings,
            baseline_advisories=baseline_advisories,
            current_advisories=current_advisories,
        )

    inputs = {
        "boms": str(Path(args.boms)),
        "current_advisories": str(Path(args.advisories)),
        "applicability_records": str(Path(args.records)),
    }
    if args.baseline_advisories:
        inputs["baseline_advisories"] = str(Path(args.baseline_advisories))

    report = build_report(
        boms, current_findings,
        changes=changes, baseline_findings=baseline_findings, inputs=inputs,
    )
    json_text = render_json(report)
    text_text = render_text(report)

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json_text + "\n", encoding="utf-8")
    if args.text_out:
        out_path = Path(args.text_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text_text + "\n", encoding="utf-8")
    if not args.json_out and not args.text_out:
        # 默认把文本报告打到标准输出，JSON 仍可通过显式路径落盘
        sys.stdout.write(text_text + "\n")
    else:
        sys.stdout.write(
            f"裁决完成：{len(current_findings)} 条条目，"
            f"变化 {0 if changes is None else len(changes)} 条\n"
        )
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except (OSError, ValueError) as exc:
        # 输入文件缺失、JSON 畸形、结构校验失败等均以 2 退出，信息单行可读
        sys.stderr.write(f"裁决器输入错误: {exc}\n")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
