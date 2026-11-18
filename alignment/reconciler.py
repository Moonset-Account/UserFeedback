"""对齐组件:把压测运行记录同版本要求的样本清单核对。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple

from .model import Expectation, Result, RunRecord, SampleSpec


class EntryStatus(Enum):
    PASS = "pass"        # 记录满足清单要求
    FAIL = "fail"        # 记录违反清单要求(应被阻止上线)
    PENDING = "pending"  # 计时被采集器中断,留待补证
    MISSING = "missing"  # 清单条目没有任何运行记录


class SubmitStatus(Enum):
    STORED = "stored"          # 首条记录,已入库
    GAP_FILLED = "gap_filled"  # 重跑填补了中断留下的补证缺口
    REJECTED_UNRELEASED_OVERWRITE = "rejected_unreleased_overwrite"  # 成功重跑试图覆盖未释放资源的记录,被拒绝
    REJECTED_DUPLICATE = "rejected_duplicate"  # 非缺口场景下的重复提交


@dataclass(frozen=True)
class SubmitOutcome:
    status: SubmitStatus
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.status in (SubmitStatus.STORED, SubmitStatus.GAP_FILLED)


@dataclass(frozen=True)
class EntryResult:
    spec: SampleSpec
    status: EntryStatus
    reasons: Tuple[str, ...] = ()
    record: Optional[RunRecord] = None


@dataclass(frozen=True)
class AlignmentReport:
    """对齐报告:每个清单条目一条结论,外加清单外的意外记录。"""

    entries: Tuple[EntryResult, ...]
    unexpected: Tuple[RunRecord, ...] = ()

    def by_status(self, status: EntryStatus) -> Tuple[EntryResult, ...]:
        return tuple(e for e in self.entries if e.status is status)

    @property
    def failures(self) -> Tuple[EntryResult, ...]:
        return self.by_status(EntryStatus.FAIL)

    @property
    def pending(self) -> Tuple[EntryResult, ...]:
        return self.by_status(EntryStatus.PENDING)

    @property
    def is_green(self) -> bool:
        """全部条目通过且无意外记录时才允许上线。"""
        return (
            all(e.status is EntryStatus.PASS for e in self.entries)
            and not self.unexpected
        )


class Reconciler:
    """把运行记录同样本清单对齐的组件。

    规则:
    - 预算按进程组合核对;兼容样本必须在限额内完成(峰值 <= 预算)。
    - 超限样本必须命中限额、在规定阶段终止,且清理事件确认资源已释放。
    - 计时被采集器中断的记录留待补证(PENDING),重跑可补缺。
    - 成功的重跑不能覆盖同条件下未释放资源的记录。
    """

    def __init__(self, specs: Iterable[SampleSpec] = ()) -> None:
        self._specs: List[SampleSpec] = []
        self._spec_by_key: Dict[tuple, SampleSpec] = {}
        self._records: Dict[tuple, RunRecord] = {}
        for spec in specs:
            self.add_spec(spec)

    def add_spec(self, spec: SampleSpec) -> None:
        if spec.key in self._spec_by_key:
            raise ValueError(f"duplicate manifest entry for {spec.key!r}")
        self._specs.append(spec)
        self._spec_by_key[spec.key] = spec

    # ------------------------------------------------------------------ submit

    def submit(self, record: RunRecord) -> SubmitOutcome:
        """登记一条运行记录;处理重跑与防覆盖守卫。"""
        existing = self._records.get(record.key)
        if existing is None:
            self._records[record.key] = record
            return SubmitOutcome(SubmitStatus.STORED)

        if existing.result is Result.INTERRUPTED:
            # 中断记录留待补证,重跑可补缺
            self._records[record.key] = record
            return SubmitOutcome(SubmitStatus.GAP_FILLED, "rerun filled interrupted record")

        if not existing.resources_released and self._is_success(record):
            # 成功的重跑不能覆盖同条件下未释放资源的记录
            return SubmitOutcome(
                SubmitStatus.REJECTED_UNRELEASED_OVERWRITE,
                "successful rerun cannot overwrite a record with unreleased resources",
            )

        return SubmitOutcome(
            SubmitStatus.REJECTED_DUPLICATE,
            "record already final; only interrupted (pending) records may be refilled",
        )

    def _is_success(self, record: RunRecord) -> bool:
        """判定一条记录是否为"成功的重跑"(限额内完成)。"""
        if record.result is not Result.COMPLETED or record.limit_hit:
            return False
        spec = self._spec_by_key.get(record.key)
        if spec is not None and record.memory_peak > spec.memory_budget:
            return False
        return True

    # ------------------------------------------------------------------- align

    def align(self) -> AlignmentReport:
        entries = tuple(
            self._evaluate(spec, self._records.get(spec.key)) for spec in self._specs
        )
        unexpected = tuple(
            record for key, record in self._records.items() if key not in self._spec_by_key
        )
        return AlignmentReport(entries=entries, unexpected=unexpected)

    def _evaluate(self, spec: SampleSpec, record: Optional[RunRecord]) -> EntryResult:
        if record is None:
            return EntryResult(spec, EntryStatus.MISSING, ("no run record submitted",))
        if record.result is Result.INTERRUPTED:
            return EntryResult(
                spec,
                EntryStatus.PENDING,
                ("timing interrupted by collector; awaiting rerun evidence",),
                record,
            )

        reasons: List[str] = []
        if spec.expectation is Expectation.COMPATIBLE:
            # 兼容样本:限额内完成,预算按进程组合核对
            if record.result is not Result.COMPLETED:
                reasons.append(
                    f"compatible sample must complete, got {record.result.value}"
                )
            if record.limit_hit:
                reasons.append("compatible sample hit the limit")
            if record.memory_peak > spec.memory_budget:
                reasons.append(
                    f"memory peak {record.memory_peak} exceeds budget "
                    f"{spec.memory_budget} for process combo {spec.process_combo!r}"
                )
        else:
            # 超限样本:命中限额、在规定阶段终止并释放资源
            if not record.limit_hit:
                reasons.append("over-limit sample did not hit the limit")
            if record.result is not Result.TERMINATED:
                reasons.append(
                    f"over-limit sample must be terminated, got {record.result.value}"
                )
            if record.terminated_stage != spec.required_stage:
                reasons.append(
                    f"terminated at stage {record.terminated_stage!r}, "
                    f"required {spec.required_stage!r}"
                )
        if not record.resources_released:
            reasons.append("resources not released after the run")

        status = EntryStatus.FAIL if reasons else EntryStatus.PASS
        return EntryResult(spec, status, tuple(reasons), record)
