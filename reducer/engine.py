"""缩减引擎。

两类“单次允许变换”：

1. **记录删除**：删除一条记录时，必须连同所有（传递）依赖它的
   记录一起删除，即依赖闭包；删除后重新跑结构校验，杜绝悬空引用。
2. **字段缩减**：

   * 字符串字段在不低于声明 ``min_length`` 的前提下逐字符缩短；
   * 可选字段可被整体移除；
   * 引用/引用列表字段不直接改写，其完整性由记录删除闭包保证。

引擎对每个候选先做格式校验，再在总试验预算内重复试验；只有
格式有效且 ``required_repeats`` 次试验全部复现原失败指纹的候选
才会替换当前样本。预算耗尽即停止并如实报告，绝不把未经充分
重复验证的候选宣传为最小样本。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .cache import TrialCache
from .model import FieldSpec, Record, Sample
from .tester import STATUS_MATCH, Tester, TrialResult, all_matches, safe_run
from .validator import FormatValidator, StructuralValidator

STATE_VERSION = 1


# ---------------------------------------------------------------------
# 变换描述
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class Transform:
    """单次允许变换的可复现描述。"""

    kind: str                       # "delete_records" | "truncate_field" | "remove_field"
    rids: tuple[str, ...] = ()
    field_name: str | None = None
    new_length: int | None = None

    def describe(self) -> str:
        if self.kind == "delete_records":
            return f"delete_records(sorted={sorted(self.rids)})"
        if self.kind == "truncate_field":
            return f"truncate_field(rid={self.rids[0]},field={self.field_name},len={self.new_length})"
        return f"remove_field(rid={self.rids[0]},field={self.field_name})"


@dataclass
class TransformAttempt:
    transform: Transform
    valid: bool
    accepted: bool
    trial_results: list[TrialResult] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "transform": {
                "kind": self.transform.kind,
                "rids": list(self.transform.rids),
                "field_name": self.transform.field_name,
                "new_length": self.transform.new_length,
            },
            "valid": self.valid,
            "accepted": self.accepted,
            "reason": self.reason,
            "results": [
                {"status": r.status, "fingerprint": r.fingerprint, "detail": r.detail}
                for r in self.trial_results
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TransformAttempt":
        t = data["transform"]
        return cls(
            transform=Transform(
                kind=t["kind"],
                rids=tuple(t.get("rids", [])),
                field_name=t.get("field_name"),
                new_length=t.get("new_length"),
            ),
            valid=bool(data["valid"]),
            accepted=bool(data["accepted"]),
            reason=data.get("reason", ""),
            trial_results=[
                TrialResult(
                    status=r["status"],
                    fingerprint=r.get("fingerprint"),
                    detail=r.get("detail", ""),
                )
                for r in data.get("results", [])
            ],
        )


# ---------------------------------------------------------------------
# 依赖闭包
# ---------------------------------------------------------------------
def dependency_closure(sample: Sample, root_rid: str) -> list[str]:
    """返回删除 ``root_rid`` 时必须连带删除的记录 id 闭包。

    依赖方向：记录 A 的引用字段指向 B，说明 A 依赖 B；删除 B 时
    必须连带删除 A。这里求的是“谁（传递）依赖 root”。
    """
    # 被依赖者 -> 依赖者列表
    dependents: dict[str, list[str]] = {}
    for record in sample.records:
        for target in record.reference_targets():
            dependents.setdefault(target, []).append(record.rid)

    closure: set[str] = set()
    stack = [root_rid]
    while stack:
        current = stack.pop()
        if current in closure:
            continue
        closure.add(current)
        stack.extend(dependents.get(current, []))
    return sorted(closure)


# ---------------------------------------------------------------------
# 变换生成与应用
# ---------------------------------------------------------------------
def generate_transforms(sample: Sample) -> list[Transform]:
    """按确定性顺序枚举当前样本的全部单次变换。

    顺序固定（记录顺序 -> 字段名），保证暂停恢复后的遍历一致。
    """
    transforms: list[Transform] = []

    # 1) 记录删除（含依赖闭包）
    for record in sample.records:
        closure = dependency_closure(sample, record.rid)
        transforms.append(
            Transform(kind="delete_records", rids=tuple(closure))
        )

    # 2) 字段缩减
    for record in sample.records:
        for name in sorted(record.specs):
            spec = record.specs[name]
            if name not in record.fields:
                continue
            if spec.kind == "string":
                value = record.fields[name]
                # 从当前长度 -1 递减到 min_length；不产生等价变换
                for new_len in range(len(value) - 1, spec.min_length - 1, -1):
                    transforms.append(
                        Transform(
                            kind="truncate_field",
                            rids=(record.rid,),
                            field_name=name,
                            new_length=new_len,
                        )
                    )
            elif spec.optional and spec.kind in ("string", "any"):
                transforms.append(
                    Transform(
                        kind="remove_field",
                        rids=(record.rid,),
                        field_name=name,
                    )
                )
    return transforms


def apply_transform(sample: Sample, transform: Transform) -> Sample:
    """在样本副本上应用变换，返回新样本（不改原样本）。"""
    candidate = sample.copy()
    if transform.kind == "delete_records":
        remove = set(transform.rids)
        candidate.records = [r for r in candidate.records if r.rid not in remove]
        return candidate

    target = candidate.get(transform.rids[0])
    if transform.kind == "truncate_field":
        assert transform.field_name is not None and transform.new_length is not None
        spec: FieldSpec = target.specs[transform.field_name]
        value = target.fields[transform.field_name]
        # 再次自检：不允许跌破声明的最小长度
        if transform.new_length < spec.min_length:
            raise ValueError("truncate_below_min_length")
        target.fields[transform.field_name] = value[: transform.new_length]
    elif transform.kind == "remove_field":
        assert transform.field_name is not None
        if not target.specs[transform.field_name].optional:
            raise ValueError("remove_required_field")
        target.fields.pop(transform.field_name, None)
    else:
        raise ValueError(f"unknown_transform:{transform.kind}")
    return candidate


def dedupe_transforms(transforms: list[Transform]) -> list[Transform]:
    """去重：不同根记录可能产生相同的删除闭包。"""
    seen: set[str] = set()
    unique: list[Transform] = []
    for transform in transforms:
        token = transform.describe()
        if token not in seen:
            seen.add(token)
            unique.append(transform)
    return unique


# ---------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------
@dataclass
class ReductionReport:
    accepted: list[TransformAttempt] = field(default_factory=list)
    rejected: list[TransformAttempt] = field(default_factory=list)
    invalid: list[TransformAttempt] = field(default_factory=list)
    stop_reason: str = "minimal"          # "minimal" | "budget_exhausted"
    budget: int = 0
    trials_used: int = 0
    required_repeats: int = 1
    target_fingerprint: str = ""
    initial_hash: str = ""
    final_hash: str = ""
    initial_record_count: int = 0
    final_record_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stop_reason": self.stop_reason,
            "budget": self.budget,
            "trials_used": self.trials_used,
            "trials_remaining": self.budget - self.trials_used,
            "required_repeats": self.required_repeats,
            "target_fingerprint": self.target_fingerprint,
            "initial_hash": self.initial_hash,
            "final_hash": self.final_hash,
            "initial_record_count": self.initial_record_count,
            "final_record_count": self.final_record_count,
            "accepted": [a.to_dict() for a in self.accepted],
            "rejected": [a.to_dict() for a in self.rejected],
            "invalid": [a.to_dict() for a in self.invalid],
        }


# ---------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------
class Reducer:
    """贪心缩减引擎：固定变换集、预算化试验、可暂停恢复。"""

    def __init__(
        self,
        sample: Sample,
        tester: Tester,
        validator: FormatValidator | None = None,
        target_fingerprint: str = "",
        required_repeats: int = 3,
        budget: int = 100,
        cache: TrialCache | None = None,
    ) -> None:
        if required_repeats <= 0:
            raise ValueError("required_repeats_must_be_positive")
        if budget <= 0:
            raise ValueError("budget_must_be_positive")
        self.current = sample.copy()
        self.tester = tester
        self.validator = validator or StructuralValidator()
        self.target_fingerprint = target_fingerprint
        self.required_repeats = required_repeats
        self.budget = budget
        self.trials_used = 0
        self.cache = cache or TrialCache()
        self.report = ReductionReport(
            budget=budget,
            required_repeats=required_repeats,
            target_fingerprint=target_fingerprint,
            initial_hash=sample.content_hash(),
            final_hash=sample.content_hash(),
            initial_record_count=len(sample.records),
            final_record_count=len(sample.records),
        )
        # 断点续跑：当前轮已尝试过的变换描述
        self._attempted_this_round: set[str] = set()

    # ----- 候选评估 ----------------------------------------------
    def _evaluate(self, candidate: Sample, transform: Transform) -> TransformAttempt:
        # 1) 格式校验：引用完整性、长度约束、调用方校验器
        if not self.validator.validate(candidate):
            attempt = TransformAttempt(
                transform=transform,
                valid=False,
                accepted=False,
                reason="format_invalid",
            )
            self.report.invalid.append(attempt)
            return attempt

        content_hash = candidate.content_hash()
        results: list[TrialResult] = []
        # 2) 先取已缓存的试验记录（缓存键绑定内容/版本/重复次数）
        entry = self.cache.get(
            content_hash, self.tester.version, self.required_repeats
        )
        if entry is not None:
            results.extend(entry.results)

        # 3) 预算内补齐到 required_repeats 次
        while len(results) < self.required_repeats:
            if self.trials_used >= self.budget:
                attempt = TransformAttempt(
                    transform=transform,
                    valid=True,
                    accepted=False,
                    trial_results=list(results),
                    reason="budget_exhausted_mid_evaluation",
                )
                self.report.rejected.append(attempt)
                return attempt
            result = safe_run(self.tester, candidate, self.target_fingerprint)
            results.append(result)
            self.trials_used += 1
            self.cache.append(
                content_hash,
                self.tester.version,
                self.required_repeats,
                result,
            )

        accepted = all_matches(results, self.required_repeats)
        attempt = TransformAttempt(
            transform=transform,
            valid=True,
            accepted=accepted,
            trial_results=list(results),
            reason="all_matches" if accepted else "not_all_target_fingerprints",
        )
        (self.report.accepted if accepted else self.report.rejected).append(attempt)
        return attempt

    # ----- 主循环 ------------------------------------------------
    def run(self) -> ReductionReport:
        while True:
            transforms = dedupe_transforms(generate_transforms(self.current))
            progressed = False
            for transform in transforms:
                token = transform.describe()
                if token in self._attempted_this_round:
                    continue
                self._attempted_this_round.add(token)

                candidate = apply_transform(self.current, transform)
                attempt = self._evaluate(candidate, transform)

                if attempt.reason == "budget_exhausted_mid_evaluation":
                    # 中断的变换不算“已尝试完”：恢复后必须重试，
                    # 否则可能把未经完整重复试验的候选误报为最小。
                    self._attempted_this_round.discard(token)
                    self._finalize("budget_exhausted")
                    return self.report
                if attempt.accepted:
                    # 只有通过全部重复试验的候选才能替换当前样本
                    self.current = candidate
                    progressed = True
                    self._attempted_this_round.clear()
                    break  # 在新样本上重新枚举变换

            if progressed:
                continue
            # 一轮下来没有可接受变换：
            # 若是预算打断则已在上面返回；否则为变换级最小
            self._finalize("minimal")
            return self.report

    def _finalize(self, stop_reason: str) -> None:
        self.report.stop_reason = stop_reason
        self.report.trials_used = self.trials_used
        self.report.final_hash = self.current.content_hash()
        self.report.final_record_count = len(self.current.records)

    # ----- 暂停 / 恢复 -------------------------------------------
    def to_state_dict(self) -> dict[str, Any]:
        """完整快照：当前候选、试验记录与缓存，足以从磁盘续跑。"""
        return {
            "state_version": STATE_VERSION,
            "current_sample": self.current.to_dict(),
            "target_fingerprint": self.target_fingerprint,
            "required_repeats": self.required_repeats,
            "budget": self.budget,
            "trials_used": self.trials_used,
            "tester_version": self.tester.version,
            "attempted_this_round": sorted(self._attempted_this_round),
            "report": self.report.to_dict(),
            "cache": self.cache.to_dict(),
        }

    def save_state(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_state_dict(), handle, ensure_ascii=False, indent=2)

    @classmethod
    def from_state_dict(
        cls,
        data: dict[str, Any],
        tester: Tester,
        validator: FormatValidator | None = None,
        new_budget: int | None = None,
    ) -> "Reducer":
        if int(data.get("state_version", 0)) != STATE_VERSION:
            raise ValueError("incompatible_state_version")
        required_repeats = int(data["required_repeats"])
        if tester.version != data["tester_version"]:
            raise ValueError("tester_version_mismatch_cannot_resume")
        if required_repeats <= 0 or int(data["budget"]) <= 0:
            raise ValueError("corrupt_state_budget_or_repeats")

        # 恢复时可追加总预算；None 表示沿用保存时的总预算。
        budget = int(data["budget"]) if new_budget is None else int(new_budget)
        if budget <= 0:
            raise ValueError("budget_must_be_positive")

        sample = Sample.from_dict(data["current_sample"])
        reducer = cls(
            sample=sample,
            tester=tester,
            validator=validator,
            target_fingerprint=data["target_fingerprint"],
            required_repeats=required_repeats,
            budget=budget,
            cache=TrialCache.from_dict(data.get("cache", {"entries": []})),
        )
        reducer.trials_used = int(data["trials_used"])
        reducer._attempted_this_round = set(data.get("attempted_this_round", []))
        # 恢复历史报告（accepted/rejected/invalid 全量保留）
        saved_report = data.get("report", {})
        reducer.report.accepted = [
            TransformAttempt.from_dict(d) for d in saved_report.get("accepted", [])
        ]
        reducer.report.rejected = [
            TransformAttempt.from_dict(d) for d in saved_report.get("rejected", [])
        ]
        reducer.report.invalid = [
            TransformAttempt.from_dict(d) for d in saved_report.get("invalid", [])
        ]
        return reducer

    @classmethod
    def load_state(
        cls,
        path: str,
        tester: Tester,
        validator: FormatValidator | None = None,
        new_budget: int | None = None,
    ) -> "Reducer":
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return cls.from_state_dict(
            data, tester=tester, validator=validator, new_budget=new_budget
        )
