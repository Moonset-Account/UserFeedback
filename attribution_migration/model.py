"""声明式规则、观测与旧结论的领域模型及输入解析。

所有时间统一使用 ISO-8601 UTC 字符串（如 ``2026-03-01T08:00:00Z``），
模块内部转换为带时区的 :class:`datetime.datetime` 处理。

声明式规则结构（dict 输入）
---------------------------

旧规则 ``OldRule``::

    {
      "rule_id": "OLD-A",
      "kind": "legacy",
      "conclusion_id": "legacy-a",
      "label": "旧版归因结论名称",
      "requires": [
        {"fact": "fact-x", "min_level": "confirmed", "independent_source": true}
      ]
    }

新规则 ``NewRule`` 的三种形态::

    # 1. 一对一替换
    {"rule_id": "NEW-A", "kind": "replaces", "replaces": "OLD-A",
     "conclusion": {"conclusion_id": "...", "label": "...",
                    "requires": [Requirement, ...]}}

    # 2. 拆分：一条旧规则被拆成若干需要独立来源的结论
    {"rule_id": "NEW-S", "kind": "split", "splits_from": "OLD-S",
     "conclusions": [ConclusionSpec, ...]}

    # 3. 合并：互相排斥的解释合并为一个结论（可能判定为 uncertain）
    {"rule_id": "NEW-M", "kind": "merge",
     "merges": ["OLD-M1", "OLD-M2"],
     "conclusion": {"conclusion_id": "...", "label": "...",
                    "requires": [...]},
     "branches": [ConclusionSpec, ...]}

观测 ``Observation``::

    {"obs_id": "obs-1", "fact": "fact-x", "source_id": "src-1",
     "evidence_level": "confirmed", "valid_from": "...", "valid_to": null,
     "supersedes": null, "corrected_at": null}

旧结论 ``LegacyConclusion``::

    {"legacy_rule_id": "OLD-A", "verdict": "established",
     "valid_from": "...", "valid_to": null,
     "supporting_obs_ids": ["obs-1", "obs-2"]}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

#: 证据等级，由弱到强。求值时只有 ``>= min_level`` 的观测才算满足要求。
EVIDENCE_RANK = ("rumor", "initial", "preliminary", "confirmed", "official")

#: 新规则的三种形态。
RULE_KINDS = ("replaces", "split", "merge")


class RuleValidationError(ValueError):
    """输入数据不满足声明式结构或引用完整性时抛出。"""


def parse_timestamp(value: Any, *, field_name: str) -> datetime:
    """把 ISO-8601 字符串解析为 UTC ``datetime``；``None`` 表示开放端点。"""
    if value is None:
        raise RuleValidationError(f"字段 {field_name} 不能为空")
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise RuleValidationError(
                f"字段 {field_name} 不是合法的 ISO-8601 时间: {value!r}"
            ) from exc
    else:
        raise RuleValidationError(f"字段 {field_name} 必须是时间字符串: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_timestamp(dt: Optional[datetime]) -> Optional[str]:
    """输出 UTC ``Z`` 结尾的 ISO-8601 字符串；开放端点返回 ``None``。"""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def evidence_rank(level: str) -> int:
    try:
        return EVIDENCE_RANK.index(level)
    except ValueError as exc:
        raise RuleValidationError(
            f"未知证据等级 {level!r}，合法值: {', '.join(EVIDENCE_RANK)}"
        ) from exc


def _require_mapping(data: Any, name: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise RuleValidationError(f"{name} 必须是对象(dict)，实际为 {type(data).__name__}")
    return data


def _require_nonempty_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuleValidationError(f"字段 {field_name} 必须是非空字符串")
    return value


@dataclass(frozen=True)
class Requirement:
    """单条事实要求。

    :param fact: 需要匹配的观测事实谓词。
    :param min_level: 最低证据等级，含本等级。
    :param independent_source: 为 ``True`` 时该要求与其它带此标记的要求
        必须由不同 ``source_id`` 的观测满足（独立来源）。
    """

    fact: str
    min_level: str
    independent_source: bool = False

    @classmethod
    def from_dict(cls, data: Any) -> "Requirement":
        data = _require_mapping(data, "Requirement")
        fact = _require_nonempty_str(data.get("fact"), "Requirement.fact")
        min_level = _require_nonempty_str(
            data.get("min_level"), "Requirement.min_level"
        )
        evidence_rank(min_level)  # 校验等级合法性
        independent = bool(data.get("independent_source", False))
        return cls(fact=fact, min_level=min_level, independent_source=independent)


@dataclass(frozen=True)
class ConclusionSpec:
    """新规则产出的结论规格（结论标识 + 证据要求集合）。"""

    conclusion_id: str
    label: str
    requires: tuple[Requirement, ...]

    @classmethod
    def from_dict(cls, data: Any) -> "ConclusionSpec":
        data = _require_mapping(data, "ConclusionSpec")
        conclusion_id = _require_nonempty_str(
            data.get("conclusion_id"), "ConclusionSpec.conclusion_id"
        )
        label = _require_nonempty_str(data.get("label"), "ConclusionSpec.label")
        raw_requires = data.get("requires")
        if not isinstance(raw_requires, list) or not raw_requires:
            raise RuleValidationError(
                f"结论 {conclusion_id} 的 requires 必须是非空列表"
            )
        requires = tuple(Requirement.from_dict(item) for item in raw_requires)
        return cls(conclusion_id=conclusion_id, label=label, requires=requires)


@dataclass(frozen=True)
class OldRule:
    """旧版声明式规则。"""

    rule_id: str
    conclusion_id: str
    label: str
    requires: tuple[Requirement, ...]

    @classmethod
    def from_dict(cls, data: Any) -> "OldRule":
        data = _require_mapping(data, "OldRule")
        rule_id = _require_nonempty_str(data.get("rule_id"), "OldRule.rule_id")
        kind = data.get("kind", "legacy")
        if kind != "legacy":
            raise RuleValidationError(
                f"旧规则 {rule_id} 的 kind 必须是 'legacy'，实际为 {kind!r}"
            )
        conclusion_id = _require_nonempty_str(
            data.get("conclusion_id"), "OldRule.conclusion_id"
        )
        label = _require_nonempty_str(data.get("label"), "OldRule.label")
        raw_requires = data.get("requires")
        if not isinstance(raw_requires, list) or not raw_requires:
            raise RuleValidationError(f"旧规则 {rule_id} 的 requires 必须是非空列表")
        requires = tuple(Requirement.from_dict(item) for item in raw_requires)
        return cls(
            rule_id=rule_id,
            conclusion_id=conclusion_id,
            label=label,
            requires=requires,
        )


@dataclass(frozen=True)
class NewRule:
    """新版声明式规则，支持 replaces / split / merge 三种形态。"""

    rule_id: str
    kind: str
    replaces: Optional[str]
    splits_from: Optional[str]
    merges: tuple[str, ...]
    conclusion: Optional[ConclusionSpec]
    conclusions: tuple[ConclusionSpec, ...]
    branches: tuple[ConclusionSpec, ...]

    @classmethod
    def from_dict(cls, data: Any) -> "NewRule":
        data = _require_mapping(data, "NewRule")
        rule_id = _require_nonempty_str(data.get("rule_id"), "NewRule.rule_id")
        kind = data.get("kind")
        if kind not in RULE_KINDS:
            raise RuleValidationError(
                f"新规则 {rule_id} 的 kind 必须是 {RULE_KINDS} 之一，实际为 {kind!r}"
            )

        replaces = data.get("replaces")
        splits_from = data.get("splits_from")
        merges = data.get("merges")
        conclusion = data.get("conclusion")
        conclusions = data.get("conclusions")
        branches = data.get("branches")

        if kind == "replaces":
            if not isinstance(replaces, str) or not replaces.strip():
                raise RuleValidationError(f"replaces 规则 {rule_id} 缺少 replaces 字段")
            if not isinstance(conclusion, dict):
                raise RuleValidationError(
                    f"replaces 规则 {rule_id} 需要单个 conclusion 规格"
                )
            spec = ConclusionSpec.from_dict(conclusion)
            return cls(
                rule_id=rule_id,
                kind=kind,
                replaces=replaces,
                splits_from=None,
                merges=(),
                conclusion=spec,
                conclusions=(),
                branches=(),
            )

        if kind == "split":
            if not isinstance(splits_from, str) or not splits_from.strip():
                raise RuleValidationError(f"split 规则 {rule_id} 缺少 splits_from 字段")
            if not isinstance(conclusions, list) or len(conclusions) < 2:
                raise RuleValidationError(
                    f"split 规则 {rule_id} 的 conclusions 至少包含两个子结论"
                )
            specs = tuple(ConclusionSpec.from_dict(item) for item in conclusions)
            return cls(
                rule_id=rule_id,
                kind=kind,
                replaces=None,
                splits_from=splits_from,
                merges=(),
                conclusion=None,
                conclusions=specs,
                branches=(),
            )

        # merge
        if not isinstance(merges, list) or not merges:
            raise RuleValidationError(f"merge 规则 {rule_id} 缺少非空 merges 列表")
        merge_ids = tuple(_require_nonempty_str(m, "NewRule.merges[]") for m in merges)
        if not isinstance(branches, list) or len(branches) != len(merge_ids):
            raise RuleValidationError(
                f"merge 规则 {rule_id} 的 branches 数量必须与 merges 一致"
            )
        branch_specs = tuple(ConclusionSpec.from_dict(item) for item in branches)
        merged_spec = ConclusionSpec.from_dict(conclusion) if conclusion else None
        return cls(
            rule_id=rule_id,
            kind=kind,
            replaces=None,
            splits_from=None,
            merges=merge_ids,
            conclusion=merged_spec,
            conclusions=(),
            branches=branch_specs,
        )


@dataclass(frozen=True)
class Observation:
    """一条原始观测及其证据等级。

    :param supersedes: 被本条观测更正/取代的旧观测 id（形成更正链）。
    :param corrected_at: 本条更正生效的时间；仅更正观测需要填写。
    """

    obs_id: str
    fact: str
    source_id: str
    evidence_level: str
    valid_from: datetime
    valid_to: Optional[datetime]
    supersedes: Optional[str] = None
    corrected_at: Optional[datetime] = None

    @classmethod
    def from_dict(cls, data: Any) -> "Observation":
        data = _require_mapping(data, "Observation")
        obs_id = _require_nonempty_str(data.get("obs_id"), "Observation.obs_id")
        fact = _require_nonempty_str(data.get("fact"), "Observation.fact")
        source_id = _require_nonempty_str(
            data.get("source_id"), "Observation.source_id"
        )
        level = _require_nonempty_str(
            data.get("evidence_level"), "Observation.evidence_level"
        )
        evidence_rank(level)
        valid_from = parse_timestamp(
            data.get("valid_from"), field_name=f"{obs_id}.valid_from"
        )
        valid_to = (
            parse_timestamp(
                data["valid_to"], field_name=f"{obs_id}.valid_to"
            )
            if data.get("valid_to") is not None
            else None
        )
        if valid_to is not None and valid_to <= valid_from:
            raise RuleValidationError(
                f"观测 {obs_id} 的 valid_to 必须晚于 valid_from"
            )
        supersedes = data.get("supersedes")
        if supersedes is not None and not isinstance(supersedes, str):
            raise RuleValidationError(f"观测 {obs_id} 的 supersedes 必须是字符串")
        corrected_at = (
            parse_timestamp(
                data["corrected_at"], field_name=f"{obs_id}.corrected_at"
            )
            if data.get("corrected_at") is not None
            else None
        )
        if supersedes is None and corrected_at is not None:
            raise RuleValidationError(
                f"观测 {obs_id} 含 corrected_at 但缺少 supersedes 关联"
            )
        if supersedes is not None and corrected_at is None:
            raise RuleValidationError(
                f"观测 {obs_id} 更正了其它观测，必须提供 corrected_at"
            )
        return cls(
            obs_id=obs_id,
            fact=fact,
            source_id=source_id,
            evidence_level=level,
            valid_from=valid_from,
            valid_to=valid_to,
            supersedes=supersedes,
            corrected_at=corrected_at,
        )


@dataclass(frozen=True)
class LegacyConclusion:
    """按旧规则已经给出的调查结论。"""

    legacy_rule_id: str
    verdict: str
    valid_from: datetime
    valid_to: Optional[datetime]
    supporting_obs_ids: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: Any) -> "LegacyConclusion":
        data = _require_mapping(data, "LegacyConclusion")
        legacy_rule_id = _require_nonempty_str(
            data.get("legacy_rule_id"), "LegacyConclusion.legacy_rule_id"
        )
        verdict = data.get("verdict")
        if verdict != "established":
            raise RuleValidationError(
                f"旧结论 {legacy_rule_id} 的 verdict 仅支持 'established'，"
                f"实际为 {verdict!r}"
            )
        valid_from = parse_timestamp(
            data.get("valid_from"), field_name=f"{legacy_rule_id}.valid_from"
        )
        valid_to = (
            parse_timestamp(
                data["valid_to"], field_name=f"{legacy_rule_id}.valid_to"
            )
            if data.get("valid_to") is not None
            else None
        )
        if valid_to is not None and valid_to <= valid_from:
            raise RuleValidationError(
                f"旧结论 {legacy_rule_id} 的 valid_to 必须晚于 valid_from"
            )
        supporting = data.get("supporting_obs_ids")
        if not isinstance(supporting, list) or not supporting:
            raise RuleValidationError(
                f"旧结论 {legacy_rule_id} 必须提供非空 supporting_obs_ids"
            )
        ids = tuple(
            _require_nonempty_str(item, "supporting_obs_ids[]") for item in supporting
        )
        if len(set(ids)) != len(ids):
            raise RuleValidationError(
                f"旧结论 {legacy_rule_id} 的 supporting_obs_ids 存在重复"
            )
        return cls(
            legacy_rule_id=legacy_rule_id,
            verdict=verdict,
            valid_from=valid_from,
            valid_to=valid_to,
            supporting_obs_ids=ids,
        )
