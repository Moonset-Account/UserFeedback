"""命令行入口。

示例::

    python -m audit_merge \\
        --source ./src \\
        --suggestions ./suggestions.json \\
        --assertions my_assertions \\
        --output ./out \\
        --versions versions.json

``--assertions`` 指向一个可导入模块，模块内需定义 ``ASSERTIONS`` 字典
（告警编号 -> 断言可调用对象）；``--versions`` 为 文件相对路径 -> 版本号
的 JSON 映射，可省略。
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys

from .merger import Workspace, merge_suggestions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="合并两款审计插件的修复建议")
    parser.add_argument("--source", required=True, help="源码目录")
    parser.add_argument("--suggestions", required=True, help="建议组 JSON 文件")
    parser.add_argument("--output", help="合并结果输出目录（省略则只打印报告）")
    parser.add_argument("--assertions", help="保护断言模块，内含 ASSERTIONS 字典")
    parser.add_argument("--versions", help="文件版本号 JSON 映射文件")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with open(args.suggestions, "r", encoding="utf-8") as fh:
        suggestions = json.load(fh)
    versions = None
    if args.versions:
        with open(args.versions, "r", encoding="utf-8") as fh:
            versions = json.load(fh)
    assertions = {}
    if args.assertions:
        module = importlib.import_module(args.assertions)
        assertions = getattr(module, "ASSERTIONS")
    workspace = Workspace.from_directory(args.source, versions)
    report = merge_suggestions(workspace, suggestions, assertions, output_dir=args.output)
    summary = {
        "ok": report.ok,
        "applied_groups": report.applied_groups,
        "withdrawals": [w.__dict__ for w in report.withdrawals],
        "conflicts": [
            {
                "file": c.file,
                "left_group": c.left_group,
                "right_group": c.right_group,
            }
            for c in report.conflicts
        ],
        "dedup": [
            {
                "file": d.file,
                "kept_group": d.kept_group,
                "merged_groups": list(d.merged_groups),
            }
            for d in report.dedup
        ],
        "clusters": [list(c) for c in report.clusters],
        "recorded_alerts": report.recorded_alerts,
        "output_dir": report.output_dir,
    }
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
