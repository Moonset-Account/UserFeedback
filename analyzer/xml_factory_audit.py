"""离线 XML 解析工厂外部资源限制分析器。

输入：data/offline 下的清单 JSON（策略、工厂、环境覆盖、调用点/入口）。
输出：每个导入入口在每个环境中的有效设置与判定：
  - compliant                外部资源限制完整
  - explicit_deviation       配置明确偏离要求
  - evidence_missing         证据缺失（含无法解析的动态工厂）

判定与覆盖来源完全由离线清单推导，不执行任何被分析代码。
仅使用 Python 标准库；建议以 `python3 -I` 运行。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

# 判定常量
VERDICT_COMPLIANT = "compliant"
VERDICT_DEVIATION = "explicit_deviation"
VERDICT_MISSING = "evidence_missing"

VERDICT_RANK = {
    VERDICT_COMPLIANT: 0,
    VERDICT_DEVIATION: 1,
    VERDICT_MISSING: 2,
}


class ManifestError(Exception):
    """清单结构或引用错误。"""


def load_manifest(data_dir: Path) -> dict[str, Any]:
    """加载并拼接离线清单文件。"""
    required = [
        "manifest-00-policy.json",
        "manifest-01-factories.json",
        "manifest-02-environments.json",
        "manifest-03-callsites.json",
    ]
    merged: dict[str, Any] = {}
    for name in required:
        path = data_dir / name
        if not path.is_file():
            raise ManifestError(f"缺少清单文件: {path}")
        with path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
        for key, value in doc.items():
            if key == "schema_version" or key == "description":
                continue
            if key in merged:
                raise ManifestError(f"清单键冲突: {key}（来自 {name}）")
            merged[key] = value
    return merged


def build_factory_index(factories: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """factory_id -> 工厂定义，并校验别名唯一。"""
    index: dict[str, dict[str, Any]] = {}
    alias_to_id: dict[str, str] = {}
    for factory in factories:
        fid = factory["factory_id"]
        if fid in index:
            raise ManifestError(f"重复 factory_id: {fid}")
        index[fid] = factory
        for alias in factory.get("aliases", []):
            if alias in alias_to_id and alias_to_id[alias] != fid:
                raise ManifestError(
                    f"别名 {alias} 同时映射到 {alias_to_id[alias]} 与 {fid}"
                )
            alias_to_id[alias] = fid
    index["__alias_to_id__"] = alias_to_id  # 类型上与工厂定义区分，键名保留
    return index


def resolve_factory_ref(
    factory_ref: str, factory_index: dict[str, dict[str, Any]]
) -> str | None:
    """把别名或 factory_id 归一到 factory_id；无法解析返回 None。"""
    if factory_ref in factory_index and factory_ref != "__alias_to_id__":
        return factory_ref
    alias_to_id = factory_index["__alias_to_id__"]
    return alias_to_id.get(factory_ref)


def resolve_defaults(
    factory_id: str, factory_index: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """沿 parent_factory 继承默认值。

    返回 (合并后的默认值, 每个设置的来源证据链)。
    子工厂默认值覆盖父工厂同名设置；检测继承环。
    """
    chain: list[str] = []
    current = factory_id
    while current is not None:
        if current in chain:
            raise ManifestError(
                f"工厂继承存在环: {' -> '.join(chain + [current])}"
            )
        if current not in factory_index or current == "__alias_to_id__":
            raise ManifestError(f"parent_factory 引用了不存在的工厂: {current}")
        chain.append(current)
        current = factory_index[current].get("parent_factory")

    # 从根工厂向子工厂依次应用，子覆盖父
    effective: dict[str, Any] = {}
    provenance: dict[str, dict[str, str]] = {}
    for fid in reversed(chain):
        definition = factory_index[fid]
        for setting, value in definition.get("defaults", {}).items():
            effective[setting] = value
            provenance[setting] = {
                "factory_id": fid,
                "defined_at": definition.get("defined_at", ""),
                "layer": "factory-default",
            }
    evidence = [
        {"setting": setting, **origin} for setting, origin in provenance.items()
    ]
    return effective, evidence


def build_override_index(
    environments: list[dict[str, Any]],
) -> dict[str, dict[str, dict[str, list[dict[str, Any]]]]]:
    """environment -> factory_id -> setting -> [override, ...]。"""
    index: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    for env in environments:
        env_id = env["environment_id"]
        index.setdefault(env_id, {})
        for override in env.get("overrides", []):
            fid = override["factory_id"]
            setting = override["setting"]
            index[env_id].setdefault(fid, {}).setdefault(setting, []).append(
                override
            )
    return index


def detect_precedence_conflicts(
    override_index: dict[str, Any],
) -> list[dict[str, Any]]:
    """同一 environment+factory+setting 存在多条同最高 precedence 的覆盖即冲突。"""
    conflicts: list[dict[str, Any]] = []
    for env_id, by_factory in override_index.items():
        for fid, by_setting in by_factory.items():
            for setting, overrides in by_setting.items():
                if len(overrides) < 2:
                    continue
                max_prec = max(o["precedence"] for o in overrides)
                winners = [o for o in overrides if o["precedence"] == max_prec]
                if len(winners) > 1:
                    conflicts.append(
                        {
                            "environment_id": env_id,
                            "factory_id": fid,
                            "setting": setting,
                            "max_precedence": max_prec,
                            "sources": [
                                {
                                    "override_id": o["override_id"],
                                    "value": o["value"],
                                    "source": o["source"],
                                    "layer": o["layer"],
                                    "precedence": o["precedence"],
                                }
                                for o in winners
                            ],
                        }
                    )
    return conflicts


def effective_settings_for_env(
    factory_id: str,
    env_id: str,
    factory_index: dict[str, dict[str, Any]],
    override_index: dict[str, Any],
    layer_order: list[str],
) -> dict[str, Any]:
    """推导某工厂在某环境中的有效设置及逐项来源。"""
    defaults, default_evidence = resolve_defaults(factory_id, factory_index)
    provenance: dict[str, dict[str, str]] = {
        item["setting"]: {k: v for k, v in item.items() if k != "setting"}
        for item in default_evidence
    }
    effective = dict(defaults)
    applied_overrides: list[dict[str, Any]] = []
    conflict_notes: list[dict[str, Any]] = []

    env_overrides = override_index.get(env_id, {}).get(factory_id, {})
    for setting, overrides in env_overrides.items():
        max_prec = max(o["precedence"] for o in overrides)
        winners = [o for o in overrides if o["precedence"] == max_prec]
        chosen = winners[0]
        if len(winners) > 1:
            # 确定性兜底：同 precedence 按 layer_order 次序，仍无法区分则取清单首条
            ordered = sorted(
                winners,
                key=lambda o: (
                    layer_order.index(o["layer"])
                    if o["layer"] in layer_order
                    else len(layer_order)
                ),
            )
            chosen = ordered[0]
            conflict_notes.append(
                {
                    "setting": setting,
                    "message": (
                        f"{env_id}/{factory_id}/{setting} 存在 {len(winners)} 条 "
                        f"precedence={max_prec} 的覆盖，已按 layer_order 兜底选择 "
                        f"{chosen['override_id']}；来源: "
                        + ", ".join(o["source"] for o in winners)
                    ),
                    "conflicting_override_ids": [o["override_id"] for o in winners],
                }
            )
        effective[setting] = chosen["value"]
        provenance[setting] = {
            "factory_id": factory_id,
            "defined_at": chosen["source"],
            "layer": chosen["layer"],
            "override_id": chosen["override_id"],
            "precedence": str(chosen["precedence"]),
        }
        applied_overrides.append(
            {
                "setting": setting,
                "override_id": chosen["override_id"],
                "value": chosen["value"],
                "source": chosen["source"],
                "layer": chosen["layer"],
                "precedence": chosen["precedence"],
            }
        )

    return {
        "effective_settings": effective,
        "provenance": provenance,
        "applied_overrides": applied_overrides,
        "conflict_notes": conflict_notes,
    }


def evaluate(
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """主分析流程。"""
    policy = manifest["policy"]
    required: dict[str, dict[str, Any]] = policy["required_settings"]
    environments_in_scope: list[str] = policy["environments_in_scope"]

    factory_index = build_factory_index(manifest["factories"])
    override_index = build_override_index(manifest["environments"])
    layer_order = manifest.get("layer_order_for_tiebreak", [])
    precedence_conflicts = detect_precedence_conflicts(override_index)

    callsite_index = {cs["callsite_id"]: cs for cs in manifest["callsites"]}

    entry_reports: list[dict[str, Any]] = []
    for entry in manifest["entry_points"]:
        factory_ref = entry["factory_ref"]
        resolved_id = resolve_factory_ref(factory_ref, factory_index)

        base_report: dict[str, Any] = {
            "entry_id": entry["entry_id"],
            "import_name": entry["import_name"],
            "callsite_chain": [
                {
                    "callsite_id": cs_id,
                    "symbol": callsite_index[cs_id]["symbol"],
                    "file": callsite_index[cs_id]["file"],
                    "line": callsite_index[cs_id]["line"],
                    "action": callsite_index[cs_id]["action"],
                }
                for cs_id in entry["callsite_chain"]
            ],
            "factory_ref": factory_ref,
            "resolved_factory_id": resolved_id,
            "alias_normalized": resolved_id is not None
            and factory_ref != resolved_id,
            "fix_location": entry["fix_location"],
        }

        # 动态/无法解析工厂：证据缺失，不继续推导设置
        if resolved_id is None:
            base_report["factory_kind"] = "unresolved"
            base_report["environments"] = [
                {
                    "environment_id": env_id,
                    "verdict": VERDICT_MISSING,
                    "reason": "dynamic_factory_unresolved",
                    "detail": (
                        "工厂引用无法在离线清单中解析到任何 factory_id"
                        "（运行期类名/动态创建），无有效设置证据"
                    ),
                    "settings": [],
                    "conflict_notes": [],
                }
                for env_id in environments_in_scope
            ]
            base_report["entry_verdict"] = VERDICT_MISSING
            entry_reports.append(base_report)
            continue

        factory = factory_index[resolved_id]
        base_report["factory_kind"] = factory.get("kind", "static")
        if factory.get("kind") == "dynamic":
            offline_target = factory.get("resolution", {}).get(
                "offline_resolved_factory_id"
            )
            if not offline_target:
                base_report["environments"] = [
                    {
                        "environment_id": env_id,
                        "verdict": VERDICT_MISSING,
                        "reason": "dynamic_factory_unresolved",
                        "detail": (
                            "动态工厂 offline_resolved_factory_id 为空: "
                            + factory.get("resolution", {}).get(
                                "resolution_note", "无离线解析结果"
                            )
                        ),
                        "settings": [],
                        "conflict_notes": [],
                    }
                    for env_id in environments_in_scope
                ]
                base_report["entry_verdict"] = VERDICT_MISSING
                entry_reports.append(base_report)
                continue
            # 动态工厂已在离线清单中静态绑定到目标工厂，跟随到目标定义继续评估
            if offline_target not in factory_index or offline_target == "__alias_to_id__":
                raise ManifestError(
                    f"{resolved_id} 的 offline_resolved_factory_id 指向不存在的工厂: "
                    f"{offline_target}"
                )
            base_report["dynamic_resolved_to"] = offline_target
            resolved_id = offline_target

        env_reports: list[dict[str, Any]] = []
        for env_id in environments_in_scope:
            derived = effective_settings_for_env(
                resolved_id,
                env_id,
                factory_index,
                override_index,
                layer_order,
            )
            effective = derived["effective_settings"]
            provenance = derived["provenance"]
            conflicted_settings = {
                note["setting"] for note in derived["conflict_notes"]
            }

            setting_reports: list[dict[str, Any]] = []
            has_deviation = False
            has_missing = False
            for setting, requirement in required.items():
                origin = provenance.get(setting)
                if setting not in effective or origin is None:
                    has_missing = True
                    setting_reports.append(
                        {
                            "setting": setting,
                            "required_value": requirement["required_value"],
                            "effective_value": None,
                            "status": "missing_evidence",
                            "origin": None,
                            "rationale": requirement["rationale"],
                        }
                    )
                    continue
                effective_value = effective[setting]
                meets = effective_value == requirement["required_value"]
                if setting in conflicted_settings:
                    # 覆盖顺序冲突：有效值无法确定，且候选值中存在违反策略的分支
                    has_deviation = True
                    status = "override_conflict"
                elif not meets:
                    has_deviation = True
                    status = "explicit_deviation"
                else:
                    status = "meets"
                setting_reports.append(
                    {
                        "setting": setting,
                        "required_value": requirement["required_value"],
                        "effective_value": effective_value,
                        "status": status,
                        "origin": origin,
                        "rationale": requirement["rationale"],
                    }
                )

            if conflicted_settings:
                verdict = VERDICT_DEVIATION
                reason = "override_precedence_conflict_undetermined_effective_value"
            elif has_deviation:
                verdict = VERDICT_DEVIATION
                reason = "one_or_more_settings_explicitly_violate_policy"
            elif has_missing:
                verdict = VERDICT_MISSING
                reason = "one_or_more_required_settings_have_no_evidence"
            else:
                verdict = VERDICT_COMPLIANT
                reason = "all_required_settings_meet_policy"

            env_reports.append(
                {
                    "environment_id": env_id,
                    "verdict": verdict,
                    "reason": reason,
                    "settings": setting_reports,
                    "applied_overrides": derived["applied_overrides"],
                    "conflict_notes": derived["conflict_notes"],
                }
            )

        base_report["environments"] = env_reports
        base_report["entry_verdict"] = max(
            (e["verdict"] for e in env_reports),
            key=lambda v: VERDICT_RANK[v],
        )
        entry_reports.append(base_report)

    summary = {
        "total_entries": len(entry_reports),
        "by_verdict": {
            VERDICT_COMPLIANT: sum(
                1 for e in entry_reports if e["entry_verdict"] == VERDICT_COMPLIANT
            ),
            VERDICT_DEVIATION: sum(
                1 for e in entry_reports if e["entry_verdict"] == VERDICT_DEVIATION
            ),
            VERDICT_MISSING: sum(
                1 for e in entry_reports if e["entry_verdict"] == VERDICT_MISSING
            ),
        },
        "precedence_conflict_count": len(precedence_conflicts),
    }

    return {
        "policy_id": policy["policy_id"],
        "environments_in_scope": environments_in_scope,
        "summary": summary,
        "precedence_conflicts": precedence_conflicts,
        "entry_reports": entry_reports,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """把分析结果渲染成可用于补丁复查的 Markdown 报告。"""
    lines: list[str] = []
    lines.append("# XML 解析工厂外部资源限制离线分析报告")
    lines.append("")
    lines.append(f"- 策略: `{report['policy_id']}`")
    lines.append(f"- 覆盖环境: {', '.join(report['environments_in_scope'])}")
    summary = report["summary"]
    lines.append(
        "- 入口统计: 共 {total} 个；完整合规 {ok}，明确偏离 {dev}，证据缺失 {miss}".format(
            total=summary["total_entries"],
            ok=summary["by_verdict"][VERDICT_COMPLIANT],
            dev=summary["by_verdict"][VERDICT_DEVIATION],
            miss=summary["by_verdict"][VERDICT_MISSING],
        )
    )
    lines.append(
        f"- 覆盖顺序冲突: {summary['precedence_conflict_count']} 处"
    )
    lines.append("")

    lines.append("## 覆盖顺序冲突（具体来源）")
    lines.append("")
    if not report["precedence_conflicts"]:
        lines.append("无。")
    for conflict in report["precedence_conflicts"]:
        lines.append(
            f"- `{conflict['environment_id']}` / `{conflict['factory_id']}` / "
            f"`{conflict['setting']}`：{len(conflict['sources'])} 条覆盖同处 "
            f"precedence={conflict['max_precedence']}，取值互不相同："
        )
        for source in conflict["sources"]:
            lines.append(
                f"  - `{source['override_id']}` 值={source['value']} "
                f"来源=`{source['source']}` layer=`{source['layer']}`"
            )
    lines.append("")

    verdict_label = {
        VERDICT_COMPLIANT: "外部资源限制完整",
        VERDICT_DEVIATION: "配置明确偏离要求",
        VERDICT_MISSING: "证据缺失",
    }

    lines.append("## 入口明细（含配置链与修复位置）")
    lines.append("")
    for entry in report["entry_reports"]:
        lines.append(f"### {entry['entry_id']} — {entry['import_name']}")
        lines.append("")
        lines.append(f"- 入口判定: **{verdict_label[entry['entry_verdict']]}**")
        chain = " -> ".join(
            f"{cs['callsite_id']}({cs['symbol']} @ {cs['file']}:{cs['line']})"
            for cs in entry["callsite_chain"]
        )
        lines.append(f"- 调用链: {chain}")
        alias_note = (
            "（别名归一）" if entry.get("alias_normalized") else "（直接引用）"
        )
        lines.append(
            f"- 工厂引用: `{entry['factory_ref']}` -> "
            f"`{entry['resolved_factory_id']}` {alias_note}"
        )
        lines.append(f"- 修复位置: `{entry['fix_location']}`")
        lines.append("")
        for env_report in entry["environments"]:
            lines.append(
                f"#### 环境 `{env_report['environment_id']}`: "
                f"{verdict_label[env_report['verdict']]} ({env_report['reason']})"
            )
            lines.append("")
            if not env_report["settings"]:
                lines.append(f"- {env_report['detail']}")
                lines.append("")
                continue
            lines.append("| 设置项 | 要求值 | 有效值 | 状态 | 来源 |")
            lines.append("| --- | --- | --- | --- | --- |")
            status_label = {
                "meets": "满足",
                "explicit_deviation": "明确偏离",
                "override_conflict": "覆盖冲突(有效值未定)",
                "missing_evidence": "证据缺失",
            }
            for item in env_report["settings"]:
                origin = item["origin"]
                if origin is None:
                    origin_text = "（无证据）"
                else:
                    origin_text = (
                        f"{origin.get('override_id', origin.get('factory_id'))} "
                        f"@ {origin['defined_at']} [{origin['layer']}]"
                    )
                lines.append(
                    "| `{setting}` | {required} | {effective} | {status} | {origin} |".format(
                        setting=item["setting"],
                        required=item["required_value"],
                        effective=item["effective_value"],
                        status=status_label.get(item["status"], item["status"]),
                        origin=origin_text,
                    )
                )
            lines.append("")
            for note in env_report["conflict_notes"]:
                lines.append(f"> ⚠️ 覆盖冲突: {note['message']}")
                lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="离线推导导入入口 XML 工厂在各环境中的有效安全设置"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "offline",
        help="离线清单目录",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="机器可读 JSON 报告输出路径",
    )
    parser.add_argument(
        "--md-out",
        type=Path,
        default=None,
        help="Markdown 报告输出路径",
    )
    args = parser.parse_args()

    manifest = load_manifest(args.data_dir)
    report = evaluate(manifest)
    payload = json.dumps(report, ensure_ascii=False, indent=2)

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(payload, encoding="utf-8")
    if args.md_out:
        args.md_out.parent.mkdir(parents=True, exist_ok=True)
        args.md_out.write_text(render_markdown(report), encoding="utf-8")
    if not args.json_out and not args.md_out:
        print(payload)


if __name__ == "__main__":
    main()
