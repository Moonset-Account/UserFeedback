"""命令行入口：分析操作图 JSON 并输出预算门禁结论。

示例:
    python3 -m budget_guard examples/operation_graph.json \\
        --evidence examples/evidence.json \\
        --max-bytes 1048576 --max-records 1000

退出码:
    0  门禁通过
    3  门禁拒绝（超预算、存在 blocking Issue 或 strict 模式下有 warning）
    4  输入图/证据不合法或读取失败
    2  命令行参数错误（argparse 默认）
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from . import __version__, analyze, evaluate
from .errors import GraphValidationError


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="budget_guard",
        description="受限操作图静态资源预算分析（内存字节 / 展开记录数）",
    )
    p.add_argument("graph", help="操作图 JSON 文件路径")
    p.add_argument("--evidence", help="外部证据 JSON 文件路径", default=None)
    p.add_argument("--max-bytes", type=int, default=None, help="内存预算（字节）")
    p.add_argument(
        "--max-records", type=int, default=None, help="展开记录数预算"
    )
    p.add_argument(
        "--reject",
        action="append",
        default=[],
        metavar="REGION_ID",
        help="拒绝指定分支/区域 id，可重复传入",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="把活动 warning（匿名引用、未证明共享等）也视为拒绝",
    )
    p.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="输出格式（默认 text）",
    )
    p.add_argument("--version", action="version", version=f"budget_guard {__version__}")
    return p


def _render_text(decision) -> str:
    rep = decision.report
    lines: list[str] = []
    t = decision.total

    def fmt(v: Optional[int]) -> str:
        return "无有限上界(None)" if v is None else str(v)

    lines.append("=== budget_guard 静态预算分析 ===")
    lines.append(f"内存上界 bytes   : {fmt(t.bytes)}")
    lines.append(f"记录上界 records : {fmt(t.records)}")
    if decision.max_bytes is not None:
        lines.append(f"内存预算 max_bytes   : {decision.max_bytes}")
    if decision.max_records is not None:
        lines.append(f"记录预算 max_records : {decision.max_records}")
    lines.append("")
    lines.append(f"门禁结论: {'ALLOW 允许导入' if decision.allowed else 'DENY 拒绝'}")

    if decision.violations:
        lines.append("")
        lines.append("[预算违规]")
        for v in decision.violations:
            lines.append(f"  - {v.code} [{v.dimension}] {v.message}")

    if decision.blocking_issues:
        lines.append("")
        lines.append("[阻断性问题 / 可拒绝分支]")
        for iss in decision.blocking_issues:
            lines.append(
                f"  - {iss.code} @ {iss.pos} (region={iss.region_id})\n"
                f"      {iss.message}"
            )

    if decision.rejectable_branches:
        lines.append("")
        lines.append("[可单独拒绝的 branch_id]")
        for bv in decision.rejectable_branches:
            lines.append(
                f"  - {bv.branch_id} @ {bv.pos} reasons={','.join(bv.reason_codes)}"
            )

    if decision.warnings:
        lines.append("")
        lines.append("[警告（保守计数，默认不阻断）]")
        for iss in decision.warnings:
            lines.append(f"  - {iss.code} @ {iss.pos}: {iss.message}")

    muted = [i for i in rep.issues if not i.active]
    if muted:
        lines.append("")
        lines.append(f"[已静音但保留的问题] 共 {len(muted)} 条（零迭代/已拒绝/证明共享）")
        for iss in muted:
            lines.append(
                f"  - {iss.code} @ {iss.pos} active=False mute={iss.mute_reason}"
            )

    lines.append("")
    lines.append("提示: 使用 --reject <branch_id> 可在剔除指定分支后重新评估。")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        report = analyze(args.graph, args.evidence, reject=args.reject)
        decision = evaluate(
            report,
            max_bytes=args.max_bytes,
            max_records=args.max_records,
            strict=args.strict,
        )
    except GraphValidationError as exc:
        print(f"[输入图/证据不合法] {exc}", file=sys.stderr)
        return 4
    except (ValueError, TypeError) as exc:
        # 未知 reject id、非法预算等调用错误
        print(f"[参数错误] {exc}", file=sys.stderr)
        return 4

    if args.format == "json":
        print(json.dumps(decision.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(_render_text(decision))
    return 0 if decision.allowed else 3


if __name__ == "__main__":
    raise SystemExit(main())
