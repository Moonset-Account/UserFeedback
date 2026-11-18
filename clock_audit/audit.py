"""时钟域审计器：对简化数据流执行到期判定合规检查。

核心规则：
1. 两个时间点求差时必须位于同一时钟域，并统一单位。
2. 恢复的旧进程单调期限，只有具备明确的基准转换证明（BaselineProof），
   才可参与当前代次的比较。
3. 比较中只有一侧使用有效时钟时，仍需在结论中给出另一侧的证据路径。
4. 未标明来源的数据进入待核对（PENDING）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .model import (
    UNIT_TO_NS,
    VALID_CLOCK_SOURCES,
    BaselineProof,
    ClockRead,
    Compare,
    DeadlineCompute,
    Event,
    Restore,
    Save,
)

VERDICT_ACCEPT = "ACCEPT"
VERDICT_REJECT = "REJECT"
VERDICT_PENDING = "PENDING"


@dataclass
class ValueFact:
    """某个事件产出的时间值及其来源证据。"""

    value_ns: Optional[int]  # 统一到纳秒后的值；单位缺失/未知时为 None
    source: Optional[str]
    unit: Optional[str]
    origin_generation: Optional[int]  # 时钟读取发生的进程代次
    evidence: List[str]  # 证据路径：产生该值的事件 id 链


@dataclass
class Finding:
    """一次 Compare 的审计结论。"""

    compare_id: str
    verdict: str
    reason: str
    deadline_evidence: List[str]
    now_evidence: List[str]
    diff_ns: Optional[int] = None  # 统一单位后的 deadline - now
    expired: Optional[bool] = None
    notes: List[str] = field(default_factory=list)


class AuditError(Exception):
    """数据流结构错误（如引用了未知事件）。"""


class Auditor:
    """按事件顺序摄入数据流，对每个 Compare 产出 Finding。"""

    def __init__(self) -> None:
        self._facts: Dict[str, ValueFact] = {}
        self._proofs: List[BaselineProof] = []
        self.findings: List[Finding] = []

    # ------------------------------------------------------------------ #
    # 事件摄入
    # ------------------------------------------------------------------ #
    def ingest(self, events: List[Event]) -> List[Finding]:
        for event in events:
            self.apply(event)
        return self.findings

    def apply(self, event: Event) -> None:
        if isinstance(event, ClockRead):
            self._apply_clock_read(event)
        elif isinstance(event, DeadlineCompute):
            self._apply_deadline_compute(event)
        elif isinstance(event, Save):
            self._apply_save(event)
        elif isinstance(event, Restore):
            self._apply_restore(event)
        elif isinstance(event, BaselineProof):
            self._proofs.append(event)
        elif isinstance(event, Compare):
            self.findings.append(self._evaluate_compare(event))
        else:
            raise AuditError(f"未知事件类型: {type(event)!r}")

    # ------------------------------------------------------------------ #
    # 各类事件的处理
    # ------------------------------------------------------------------ #
    def _apply_clock_read(self, event: ClockRead) -> None:
        value_ns = self._to_ns(event.value, event.unit)
        self._facts[event.event_id] = ValueFact(
            value_ns=value_ns,
            source=event.source,
            unit=event.unit,
            origin_generation=event.generation,
            evidence=[event.event_id],
        )

    def _apply_deadline_compute(self, event: DeadlineCompute) -> None:
        base = self._lookup(event.base)
        duration_ns = self._to_ns(event.duration, event.duration_unit)
        if duration_ns is None:
            raise AuditError(
                f"{event.event_id}: 期限时长单位未知: {event.duration_unit!r}"
            )
        value_ns = (
            base.value_ns + duration_ns if base.value_ns is not None else None
        )
        self._facts[event.event_id] = ValueFact(
            value_ns=value_ns,
            source=base.source,
            unit=base.unit,
            origin_generation=base.origin_generation,
            evidence=[*base.evidence, event.event_id],
        )

    def _apply_save(self, event: Save) -> None:
        target = self._lookup(event.target)
        self._facts[event.event_id] = ValueFact(
            value_ns=target.value_ns,
            source=target.source,
            unit=target.unit,
            origin_generation=target.origin_generation,
            evidence=[*target.evidence, event.event_id],
        )

    def _apply_restore(self, event: Restore) -> None:
        saved = self._lookup(event.saved)
        # 恢复不改变原始读取代次；origin_generation 仍是旧进程代次，
        # 是否可参与当前比较由 Compare 评估时的证明规则决定。
        self._facts[event.event_id] = ValueFact(
            value_ns=saved.value_ns,
            source=saved.source,
            unit=saved.unit,
            origin_generation=saved.origin_generation,
            evidence=[*saved.evidence, event.event_id],
        )

    # ------------------------------------------------------------------ #
    # 比较评估
    # ------------------------------------------------------------------ #
    def _evaluate_compare(self, event: Compare) -> Finding:
        deadline = self._lookup(event.deadline)
        now = self._lookup(event.now)
        notes: List[str] = []

        def build(verdict: str, reason: str, **kwargs) -> Finding:
            return Finding(
                compare_id=event.event_id,
                verdict=verdict,
                reason=reason,
                deadline_evidence=list(deadline.evidence),
                now_evidence=list(now.evidence),
                notes=notes,
                **kwargs,
            )

        # 规则 4：未标明来源 -> 待核对；规则 3：另一侧证据路径仍随结论给出
        unlabeled = [
            side
            for side, fact in (("deadline", deadline), ("now", now))
            if fact.source is None
        ]
        if unlabeled:
            for side, fact in (("deadline", deadline), ("now", now)):
                if fact.source is None:
                    notes.append(f"{side} 侧未标明来源，进入待核对")
                elif fact.source in VALID_CLOCK_SOURCES:
                    notes.append(
                        f"{side} 侧使用有效时钟 {fact.source!r}，"
                        f"证据路径: {' -> '.join(fact.evidence)}"
                    )
            return build(
                VERDICT_PENDING,
                "存在未标明来源的数据: " + ", ".join(unlabeled),
            )

        # 来源已标注但不在有效时钟清单内 -> 待核对
        invalid = [
            side
            for side, fact in (("deadline", deadline), ("now", now))
            if fact.source not in VALID_CLOCK_SOURCES
        ]
        if invalid:
            notes.append(
                "来源不在有效时钟清单: "
                + ", ".join(f"{s}" for s in invalid)
            )
            return build(VERDICT_PENDING, "来源无法确认为有效时钟，进入待核对")

        # 规则 1a：同一时钟域
        if deadline.source != now.source:
            notes.append(
                f"deadline 侧时钟域 {deadline.source!r} "
                f"(证据: {' -> '.join(deadline.evidence)})，"
                f"now 侧时钟域 {now.source!r} "
                f"(证据: {' -> '.join(now.evidence)})"
            )
            return build(
                VERDICT_REJECT,
                f"跨时钟域求差被阻止: {deadline.source} vs {now.source}",
            )

        # 规则 1b：统一单位
        if deadline.value_ns is None or now.value_ns is None:
            missing = []
            if deadline.value_ns is None:
                missing.append("deadline")
            if now.value_ns is None:
                missing.append("now")
            return build(
                VERDICT_PENDING,
                "单位缺失或未知，无法统一: " + ", ".join(missing),
            )
        if deadline.unit != now.unit:
            notes.append(
                f"单位已统一: deadline {deadline.unit} / now {now.unit} -> ns"
            )

        # 规则 2：旧进程单调期限需要基准转换证明
        now_ns = now.value_ns
        if (
            deadline.source == "monotonic"
            and deadline.origin_generation is not None
            and now.origin_generation is not None
            and deadline.origin_generation != now.origin_generation
        ):
            proof = self._find_proof(
                deadline.origin_generation, now.origin_generation
            )
            if proof is None:
                notes.append(
                    f"单调期限来自进程代次 {deadline.origin_generation}"
                    f"（证据: {' -> '.join(deadline.evidence)}），"
                    f"当前代次 {now.origin_generation}，无基准转换证明"
                )
                return build(
                    VERDICT_REJECT,
                    "恢复的旧进程单调期限缺少基准转换证明，比较被阻止",
                )
            offset_ns = self._to_ns(proof.offset, proof.unit)
            now_ns = now.value_ns + offset_ns
            notes.append(
                f"已按基准转换证明 {proof.event_id} 换算: "
                f"代次 {proof.from_generation} -> {proof.to_generation}, "
                f"offset={proof.offset}{proof.unit}"
            )

        diff_ns = deadline.value_ns - now_ns
        return build(
            VERDICT_ACCEPT,
            "同一时钟域且单位已统一，比较有效",
            diff_ns=diff_ns,
            expired=diff_ns <= 0,
        )

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _lookup(self, event_id: str) -> ValueFact:
        try:
            return self._facts[event_id]
        except KeyError:
            raise AuditError(f"引用了未知事件: {event_id!r}") from None

    def _find_proof(
        self, from_generation: int, to_generation: int
    ) -> Optional[BaselineProof]:
        for proof in self._proofs:
            if (
                proof.from_generation == from_generation
                and proof.to_generation == to_generation
            ):
                return proof
        return None

    @staticmethod
    def _to_ns(value: int, unit: Optional[str]) -> Optional[int]:
        factor = UNIT_TO_NS.get(unit) if unit is not None else None
        return value * factor if factor is not None else None
