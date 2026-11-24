"""变化驱动的路由隔离审查器。

工作流：
1. 装入初始路由快照与待审查的探测（源域 + 前缀段），先跑一遍基线；
2. 每应用一次路由变化，重新核对全部探测；
3. 对回退到未批准中转的前缀段，输出 Finding：
   受影响前缀、首个未授权跨域步骤、引出该问题的变化 ID。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .exceptions import UnknownDomainError
from .model import (
    Change,
    Finding,
    Probe,
    TraceResult,
    TraceStatus,
)
from .state import RoutingState
from .tracer import Tracer


@dataclass
class AuditReport:
    """一次完整审查（基线 + 全部变化）的汇总结果。"""

    baseline: dict[str, TraceResult]
    findings: tuple[Finding, ...]
    traces_by_change: dict[str, dict[str, TraceResult]] = field(default_factory=dict)

    @property
    def unauthorized_findings(self) -> tuple[Finding, ...]:
        return self.findings

    def findings_for_change(self, change_id: str) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.trigger_change_id == change_id)


class ChangeAuditor:
    def __init__(self, state: RoutingState, probes: Iterable[Probe]) -> None:
        self.state = state
        self.tracer = Tracer(state)
        self._probes: tuple[Probe, ...] = tuple(probes)
        for probe in self._probes:
            self.state.require_domain(probe.origin_domain)

        # probe_id -> 最近一次追踪结果
        self._latest: dict[str, TraceResult] = {}
        # 当前仍然成立的未授权结论（按 probe_id 去重）
        self._active: dict[str, Finding] = {}
        # 全部历史结论（含恢复后再次违规），按出现顺序保存
        self._findings: list[Finding] = []
        self._baseline: dict[str, TraceResult] = {}

    # ------------------------------------------------------------------ #
    @property
    def probes(self) -> tuple[Probe, ...]:
        return self._probes

    def run_baseline(self) -> dict[str, TraceResult]:
        """对初始快照跑一遍审查，记录基线已存在的违规（trigger=None）。"""
        self._baseline = {}
        for probe in self._probes:
            result = self.tracer.trace(probe)
            self._baseline[probe.probe_id] = result
            self._latest[probe.probe_id] = result
            if result.status is TraceStatus.UNAUTHORIZED_TRANSIT:
                assert result.unauthorized_step is not None
                finding = self._make_finding(probe, result, trigger_change_id=None)
                self._active[probe.probe_id] = finding
                self._findings.append(finding)
        return dict(self._baseline)

    def apply_change(self, change: Change) -> tuple[Finding, ...]:
        """应用一次路由变化并重新核对，返回本次“新出现”的违规。"""
        self.state.apply_change(change)
        new_findings: list[Finding] = []

        for probe in self._probes:
            result = self.tracer.trace(probe)
            self._latest[probe.probe_id] = result

            if result.status is TraceStatus.UNAUTHORIZED_TRANSIT:
                assert result.unauthorized_step is not None
                # 同一探测的违规仍在持续：不重复报；变化后新出现才报
                if probe.probe_id not in self._active:
                    finding = self._make_finding(
                        probe, result, trigger_change_id=change.change_id
                    )
                    self._active[probe.probe_id] = finding
                    self._findings.append(finding)
                    new_findings.append(finding)
            else:
                # 路径恢复合规（或变成 no-route/loop 等其他状态）：撤销活动违规
                self._active.pop(probe.probe_id, None)

        return tuple(new_findings)

    def run_all(self, changes: Iterable[Change]) -> AuditReport:
        """便捷方法：基线 + 顺序应用全部变化。"""
        baseline = self.run_baseline()
        traces_by_change: dict[str, dict[str, TraceResult]] = {}
        for change in changes:
            self.apply_change(change)
            traces_by_change[change.change_id] = dict(self._latest)
        return AuditReport(
            baseline=baseline,
            findings=tuple(self._findings),
            traces_by_change=traces_by_change,
        )

    # ------------------------------------------------------------------ #
    @property
    def active_findings(self) -> tuple[Finding, ...]:
        return tuple(self._active.values())

    @property
    def all_findings(self) -> tuple[Finding, ...]:
        return tuple(self._findings)

    @property
    def latest_traces(self) -> dict[str, TraceResult]:
        return dict(self._latest)

    @property
    def baseline_traces(self) -> dict[str, TraceResult]:
        return dict(self._baseline)

    @staticmethod
    def _make_finding(
        probe: Probe, result: TraceResult, trigger_change_id: str | None
    ) -> Finding:
        assert result.unauthorized_step is not None
        return Finding(
            probe_id=probe.probe_id,
            affected_prefix=probe.target,
            origin_domain=probe.origin_domain,
            first_unauthorized_step=result.unauthorized_step,
            trigger_change_id=trigger_change_id,
        )
