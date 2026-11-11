"""命令行入口：读取脱敏报告与构建映射，输出离线归并结果。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .clustering import cluster
from .mapping import BuildMappingIndex, MappingError
from .resolution import resolve_report


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _extract_reports(doc: Any) -> list[dict[str, Any]]:
    if isinstance(doc, list):
        reports = doc
    elif isinstance(doc, dict) and isinstance(doc.get("reports"), list):
        reports = doc["reports"]
    else:
        raise ValueError("报告文件必须是数组，或包含 reports 数组的对象")
    for report in reports:
        if not isinstance(report, dict) or not isinstance(report.get("report_id"), str):
            raise ValueError("所有报告都必须是带 report_id 字符串的对象")
    return reports


def run_merge(reports_path: Path, mapping_path: Path) -> dict[str, Any]:
    reports = _extract_reports(_load_json(reports_path))
    try:
        index = BuildMappingIndex(_load_json(mapping_path))
    except MappingError as exc:
        raise ValueError(f"构建映射无效: {exc}") from exc

    resolutions = [resolve_report(report, index) for report in reports]
    result = cluster(resolutions, reports)
    result["inputs"] = {
        "reports_file": str(reports_path),
        "mapping_file": str(mapping_path),
    }
    return result


def _print_human_summary(result: dict[str, Any], stream) -> None:
    summary = result["summary"]
    stream.write("=== 崩溃报告离线归并结果 ===\n")
    stream.write(
        f"报告总数 {summary['total_reports']}，"
        f"分组 {summary['group_count']} 个（已归并 {summary['grouped_reports']} 份），"
        f"待核查 {summary['pending_review_count']} 份\n"
    )
    for group in result["groups"]:
        attr = group["stable_attribution"]
        stream.write(
            f"\n[{group['group_id']}] {group['fault_category']} "
            f"{attr['source_file']}:{attr['source_line']} @ {attr['owning_symbol']}"
            f" — {group['member_count']} 份，构建 {len(group['builds'])} 个，"
            f"代表 {group['representative_report_id']}\n"
        )
        for member in group["members"]:
            stream.write(
                f"    - {member['report_id']} build={member['module_build_id']} "
                f"addr={member['raw_address']} depth={member['max_inline_depth']} "
                f"corroborated={member['report_corroborated']}\n"
            )
    if result["pending_review"]:
        stream.write("\n待核查（证据不足）:\n")
        for record in result["pending_review"]:
            stream.write(
                f"    - {record['report_id']} [{record['reason_code']}] "
                f"{record['reason_detail']}\n"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="离线归并同一内存安全缺陷在不同构建中的崩溃报告"
    )
    parser.add_argument("--reports", required=True, type=Path, help="脱敏崩溃报告 JSON 文件")
    parser.add_argument("--mapping", required=True, type=Path, help="配套构建映射 JSON 文件")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="归并结果输出路径（默认仅打印人类可读摘要；指定后同时写 JSON）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_merge(args.reports, args.mapping)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"错误: {exc}\n")
        return 2

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
    _print_human_summary(result, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
