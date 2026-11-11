#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线修复验收校验器。

发布看板的两类误判在本工具中被显式拒绝：
  1. 上一次提交（旧代码快照 / 旧规则版本）的通过记录被算作当前修复的证据；
  2. skipped（跳过）的测试被当作 passed（通过）。

关联规则：修复清单中的每一项必需断言，必须与测试断言结果按
（test_name, object, snapshot_id, rule_version）四元组精确匹配，
其中 snapshot_id 与 rule_version 取自修复清单本身（本次验收目标）。

退出码：
  0  全部必需断言均有有效通过证据
  1  存在无法满足的必需断言
  2  输入文件缺失 / JSON 非法 / 模式校验失败
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---- 常量 -----------------------------------------------------------------

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_NOT_RUN = "not_run"
KNOWN_STATUSES = (STATUS_PASSED, STATUS_FAILED, STATUS_SKIPPED, STATUS_NOT_RUN)

# 验收结论（原因码）
VERDICT_SATISFIED = "SATISFIED"
VERDICT_NOT_EXECUTED = "NOT_EXECUTED"
VERDICT_SKIPPED = "SKIPPED"
VERDICT_FAILED = "FAILED"
VERDICT_STALE = "STALE_EVIDENCE"
VERDICT_CONFLICT = "CONFLICTING_DUPLICATES"
UNMET_VERDICTS = frozenset(
    {
        VERDICT_NOT_EXECUTED,
        VERDICT_SKIPPED,
        VERDICT_FAILED,
        VERDICT_STALE,
        VERDICT_CONFLICT,
    }
)

VERDICT_TEXT = {
    VERDICT_SATISFIED: "通过（证据有效）",
    VERDICT_NOT_EXECUTED: "未执行，缺少当前快照证据",
    VERDICT_SKIPPED: "测试被跳过——跳过不等于通过，需补测",
    VERDICT_FAILED: "测试执行失败",
    VERDICT_STALE: "仅有过期证据（快照或规则版本不匹配），不能填补当前缺口",
    VERDICT_CONFLICT: "重复结果状态互相冲突，证据不可采信",
}

RECORD_FIELDS = ("result_id", "test_name", "object", "snapshot_id",
                 "rule_version", "status")


class InputError(Exception):
    """输入数据模式错误。"""


# ---- 数据结构 -------------------------------------------------------------


@dataclass
class RequiredAssertion:
    risk_id: str
    risk_title: str
    test_name: str
    object_id: str


@dataclass
class ItemReport:
    risk_id: str
    risk_title: str
    test_name: str
    object_id: str
    verdict: str
    reason_detail: str
    exact_evidence: list[dict[str, Any]] = field(default_factory=list)
    stale_evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def satisfied(self) -> bool:
        return self.verdict == VERDICT_SATISFIED


# ---- 输入加载与模式校验 ---------------------------------------------------


def _load_json(path: str | Path) -> Any:
    p = Path(path)
    if not p.is_file():
        raise InputError(f"文件不存在或不是普通文件: {p}")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 路径可读性格外处理
        raise InputError(f"无法读取文件 {p}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(f"JSON 解析失败 {p}: {exc}") from exc


def _check_str(obj: Any, field_name: str, where: str) -> str:
    if not isinstance(obj, str) or not obj:
        raise InputError(f"{where}: 字段 {field_name!r} 必须是非空字符串")
    return obj


def parse_manifest(data: Any) -> tuple[str, str, str, list[RequiredAssertion]]:
    if not isinstance(data, dict):
        raise InputError("修复清单根节点必须是 JSON 对象")
    manifest_id = _check_str(data.get("manifest_id"), "manifest_id", "修复清单")
    snapshot_id = _check_str(data.get("snapshot_id"), "snapshot_id", "修复清单")
    rule_version = _check_str(data.get("rule_version"), "rule_version", "修复清单")

    reqs = data.get("requirements")
    if not isinstance(reqs, list) or not reqs:
        raise InputError("修复清单: requirements 必须是非空数组")

    assertions: list[RequiredAssertion] = []
    for i, req in enumerate(reqs):
        where = f"修复清单 requirements[{i}]"
        if not isinstance(req, dict):
            raise InputError(f"{where}: 必须是对象")
        risk_id = _check_str(req.get("risk_id"), "risk_id", where)
        risk_title = str(req.get("title", ""))
        raw_assertions = req.get("assertions")
        if not isinstance(raw_assertions, list) or not raw_assertions:
            raise InputError(f"{where}: assertions 必须是非空数组")
        for j, a in enumerate(raw_assertions):
            a_where = f"{where}.assertions[{j}]"
            if not isinstance(a, dict):
                raise InputError(f"{a_where}: 必须是对象")
            assertions.append(
                RequiredAssertion(
                    risk_id=risk_id,
                    risk_title=risk_title,
                    test_name=_check_str(a.get("test_name"), "test_name", a_where),
                    object_id=_check_str(a.get("object"), "object", a_where),
                )
            )
    return manifest_id, snapshot_id, rule_version, assertions


def parse_results(data: Any) -> list[dict[str, str]]:
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        data = data["results"]
    if not isinstance(data, list):
        raise InputError("测试断言结果必须是数组，或含 results 数组的对象")

    records: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for i, rec in enumerate(data):
        where = f"测试结果 records[{i}]"
        if not isinstance(rec, dict):
            raise InputError(f"{where}: 必须是对象")
        for fname in RECORD_FIELDS:
            _check_str(rec.get(fname), fname, where)
        if rec["status"] not in KNOWN_STATUSES:
            raise InputError(
                f"{where}: 未知状态 {rec['status']!r}，"
                f"允许值: {', '.join(KNOWN_STATUSES)}"
            )
        if rec["result_id"] in seen_ids:
            raise InputError(f"{where}: result_id {rec['result_id']!r} 重复，"
                             "证据记录标识必须唯一")
        seen_ids.add(rec["result_id"])
        records.append({k: rec[k] for k in RECORD_FIELDS}
                       | {"run_id": str(rec.get("run_id", "")),
                          "recorded_at": str(rec.get("recorded_at", ""))})
    return records


# ---- 核心评估 -------------------------------------------------------------


def _stale_reason(record: dict[str, str], snapshot_id: str,
                  rule_version: str) -> str:
    snap_bad = record["snapshot_id"] != snapshot_id
    rule_bad = record["rule_version"] != rule_version
    if snap_bad and rule_bad:
        return "snapshot+rule"
    if snap_bad:
        return "snapshot"
    return "rule"


def evaluate(manifest: Any, raw_results: Any) -> dict[str, Any]:
    manifest_id, snapshot_id, rule_version, assertions = parse_manifest(manifest)
    records = parse_results(raw_results)

    # 按 (test_name, object) 建索引；同名测试覆盖不同对象时互不串用。
    index: dict[tuple[str, str], list[dict[str, str]]] = {}
    for rec in records:
        index.setdefault((rec["test_name"], rec["object"]), []).append(rec)

    items: list[ItemReport] = []
    for assertion in assertions:
        candidates = index.get((assertion.test_name, assertion.object_id), [])
        exact = [r for r in candidates
                 if r["snapshot_id"] == snapshot_id
                 and r["rule_version"] == rule_version]
        stale = [
            {"record": r, "reason": _stale_reason(r, snapshot_id, rule_version)}
            for r in candidates
            if r not in exact
        ]

        if not exact:
            if stale:
                verdict = VERDICT_STALE
                passed_stale = [s for s in stale
                                if s["record"]["status"] == STATUS_PASSED]
                if passed_stale:
                    detail = (
                        f"存在 {len(passed_stale)} 条通过记录，但均来自旧快照或旧规则版本，"
                        "当前快照/规则版本下无任何执行记录"
                    )
                else:
                    detail = "当前快照/规则版本下无执行记录，过期记录中亦无通过证据"
            else:
                verdict = VERDICT_NOT_EXECUTED
                detail = "当前快照/规则版本与该对象下找不到任何测试结果记录"
            items.append(ItemReport(
                assertion.risk_id, assertion.risk_title,
                assertion.test_name, assertion.object_id,
                verdict, detail, exact_evidence=[], stale_evidence=stale))
            continue

        statuses = {r["status"] for r in exact}
        if len(statuses) > 1:
            verdict = VERDICT_CONFLICT
            detail = (
                "同一快照/规则版本下存在 "
                + "、".join(sorted(statuses))
                + f" 共 {len(exact)} 条互相矛盾的重复结果，需去重或重新执行"
            )
        else:
            only = next(iter(statuses))
            if only == STATUS_PASSED:
                verdict = VERDICT_SATISFIED
                detail = (f"{len(exact)} 条结果一致通过"
                          if len(exact) > 1 else "当前快照/规则版本下通过")
            elif only == STATUS_FAILED:
                verdict = VERDICT_FAILED
                detail = "当前快照/规则版本下执行失败，修复未生效"
            elif only == STATUS_SKIPPED:
                verdict = VERDICT_SKIPPED
                detail = "当前快照/规则版本下测试被跳过，跳过不计为通过，必须重新执行"
            else:
                verdict = VERDICT_NOT_EXECUTED
                detail = "记录状态为 not_run，等同于未执行"
        items.append(ItemReport(
            assertion.risk_id, assertion.risk_title,
            assertion.test_name, assertion.object_id,
            verdict, detail, exact_evidence=exact, stale_evidence=stale))

    satisfied = sum(1 for it in items if it.satisfied)
    unmet = len(items) - satisfied
    conflicts = sum(1 for it in items if it.verdict == VERDICT_CONFLICT)

    return {
        "manifest_id": manifest_id,
        "snapshot_id": snapshot_id,
        "rule_version": rule_version,
        "summary": {
            "total": len(items),
            "satisfied": satisfied,
            "unmet": unmet,
            "conflicts": conflicts,
            "accepted": unmet == 0,
        },
        "items": [_item_to_dict(it) for it in items],
        "pending": [_item_to_dict(it) for it in items if not it.satisfied],
    }


def _evidence_brief(rec: dict[str, str]) -> dict[str, str]:
    return {
        "result_id": rec["result_id"],
        "run_id": rec.get("run_id", ""),
        "status": rec["status"],
        "snapshot_id": rec["snapshot_id"],
        "rule_version": rec["rule_version"],
        "recorded_at": rec.get("recorded_at", ""),
    }


def _item_to_dict(it: ItemReport) -> dict[str, Any]:
    return {
        "risk_id": it.risk_id,
        "risk_title": it.risk_title,
        "test_name": it.test_name,
        "object": it.object_id,
        "verdict": it.verdict,
        "verdict_text": VERDICT_TEXT[it.verdict],
        "satisfied": it.satisfied,
        "reason_detail": it.reason_detail,
        "exact_evidence": [_evidence_brief(r) for r in it.exact_evidence],
        "stale_evidence": [
            {"stale_reason": s["reason"], **_evidence_brief(s["record"])}
            for s in it.stale_evidence
        ],
    }


# ---- 报告渲染 -------------------------------------------------------------


def _fmt_evidence(rec: dict[str, str], indent: str) -> str:
    run = rec.get("run_id") or "-"
    when = rec.get("recorded_at") or "-"
    return (f"{indent}- {rec['result_id']}  run={run}  status={rec['status']}  "
            f"快照={rec['snapshot_id']}  规则={rec['rule_version']}  时间={when}")


def render_text(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        "=" * 68,
        "修复验收报告（离线校验）",
        "=" * 68,
        f"清单 ID      : {report['manifest_id']}",
        f"目标代码快照 : {report['snapshot_id']}",
        f"规则版本     : {report['rule_version']}",
        f"断言总数     : {s['total']}    满足: {s['satisfied']}    "
        f"待补: {s['unmet']}    冲突组: {s['conflicts']}",
        "-" * 68,
    ]
    for i, it in enumerate(report["items"], start=1):
        title = f"《{it['risk_title']}》" if it["risk_title"] else ""
        lines.append(f"[{i}/{s['total']}] 风险 {it['risk_id']}{title}")
        lines.append(f"    断言: {it['test_name']}  ->  对象 {it['object']}")
        lines.append(f"    结论: {it['verdict']} — {it['verdict_text']}")
        lines.append(f"    说明: {it['reason_detail']}")
        if it["exact_evidence"]:
            lines.append("    当前快照证据（精确匹配）:")
            # exact_evidence 已是简要字典，按原始记录格式渲染
            for ev in it["exact_evidence"]:
                lines.append(_fmt_evidence(ev, "      "))
        if it["stale_evidence"]:
            lines.append("    被拒绝的过期证据（快照/规则版本不匹配）:")
            for ev in it["stale_evidence"]:
                lines.append(
                    f"      - {ev['result_id']}  run={ev.get('run_id') or '-'}  "
                    f"status={ev['status']}  快照={ev['snapshot_id']}  "
                    f"规则={ev['rule_version']}  过期原因={ev['stale_reason']}"
                )
        lines.append("")

    lines.append("-" * 68)
    if report["pending"]:
        lines.append(f"待补测试（{len(report['pending'])} 项，含证据来源）:")
        for it in report["pending"]:
            srcs = ", ".join(ev["result_id"] for ev in it["exact_evidence"]) or "无当前记录"
            stale_srcs = ", ".join(ev["result_id"] for ev in it["stale_evidence"])
            line = (f"  - {it['risk_id']} / {it['test_name']} @ {it['object']}"
                    f"\n      原因码: {it['verdict']}"
                    f"\n      证据来源: {srcs}")
            if stale_srcs:
                line += f"\n      过期证据(不予采信): {stale_srcs}"
            lines.append(line)
        lines.append("")
        lines.append(f"验收结论: 不通过 —— {s['unmet']} 项必需断言缺少有效证据")
    else:
        lines.append("待补测试: 无")
        lines.append("验收结论: 通过 —— 全部必需断言均有当前快照/规则版本下的通过证据")
    return "\n".join(lines)


# ---- CLI ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="离线修复验收校验器：按快照/规则版本/对象精确关联测试证据")
    parser.add_argument("manifest", help="合成修复清单 JSON 路径")
    parser.add_argument("results", help="测试断言结果 JSON 路径")
    parser.add_argument("--format", choices=("text", "json"), default="text",
                        help="报告格式（默认 text）")
    parser.add_argument("--report", help="同时将报告写入该路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = _load_json(args.manifest)
        raw_results = _load_json(args.results)
        report = evaluate(manifest, raw_results)
    except InputError as exc:
        print(f"输入错误: {exc}", file=sys.stderr)
        return 2

    rendered = json.dumps(report, ensure_ascii=False, indent=2) \
        if args.format == "json" else render_text(report)
    print(rendered)
    if args.report:
        try:
            Path(args.report).write_text(rendered + "\n", encoding="utf-8")
        except OSError as exc:  # pragma: no cover
            print(f"报告写入失败: {exc}", file=sys.stderr)
            return 2
    return 0 if report["summary"]["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
