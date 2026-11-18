"""审批判定服务：当前授权、决定留痕与历史复核。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from .catalog import DirectoryCatalog, ViewResolution, resolve_view
from .model import (
    AccessRequest,
    BusinessScope,
    Decision,
    DirectoryRecord,
    Outcome,
    ResourceRule,
    ReviewOutcome,
    ReviewReport,
    require_aware,
)


@dataclass(frozen=True)
class _Evaluation:
    outcome: Outcome
    reasons: Tuple[str, ...]
    applicable_record_id: Optional[str]
    conflict: bool
    stale: bool


def _fingerprint(records: Tuple[DirectoryRecord, ...]) -> str:
    """对可见输入做规范化哈希，作为“系统当时可见输入”的证据指纹。"""
    payload = [
        {
            "record_id": r.record_id,
            "subject_id": r.subject_id,
            "department": r.department,
            "on_duty": r.on_duty,
            "effective_at": r.effective_at.isoformat(),
            "received_at": r.received_at.isoformat(),
            "supersedes": r.supersedes,
        }
        for r in records
    ]
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ApprovalService:
    """权限判定器。

    规则总览：
    - 敏感请求：部门不在允许集合、主体不在岗、目录记录冲突、适用记录超过
      新鲜度阈值、或无任何可见记录时，一律转人工（MANUAL_REVIEW）；
      否则 ALLOW。
    - 普通请求：仅按所属业务的允许范围处理；冲突/无记录导致部门无法解析
      时按安全侧 DENY；新鲜度不影响业务判定，仅作为咨询性原因留痕。
    所有决定连同当时可见输入与指纹写入只读台账，复核只追加报告。
    """

    def __init__(
        self,
        catalog: DirectoryCatalog,
        freshness_threshold: timedelta,
    ) -> None:
        if not isinstance(freshness_threshold, timedelta):
            raise TypeError("freshness_threshold 必须是 timedelta")
        if freshness_threshold <= timedelta(0):
            raise ValueError("freshness_threshold 必须为正的时间差")
        self._catalog = catalog
        self._freshness_threshold = freshness_threshold
        self._resource_rules: Dict[str, ResourceRule] = {}
        self._business_scopes: Dict[str, BusinessScope] = {}
        self._ledger: Dict[str, Decision] = {}

    def register_resource(self, rule: ResourceRule) -> None:
        if rule.resource_id in self._resource_rules:
            raise ValueError(f"资源规则重复注册: {rule.resource_id}")
        self._resource_rules[rule.resource_id] = rule

    def register_business_scope(self, scope: BusinessScope) -> None:
        if scope.business_line in self._business_scopes:
            raise ValueError(f"业务范围重复注册: {scope.business_line}")
        self._business_scopes[scope.business_line] = scope

    @property
    def decisions(self) -> Tuple[Decision, ...]:
        return tuple(self._ledger.values())

    def get_decision(self, request_id: str) -> Decision:
        return self._ledger[request_id]

    # ------------------------------------------------------------------
    # 当前授权
    # ------------------------------------------------------------------
    def decide(self, request: AccessRequest) -> Decision:
        if request.request_id in self._ledger:
            raise ValueError(f"请求 id 已存在决定，不允许覆盖: {request.request_id}")
        rule = self._resource_rules.get(request.resource_id)
        if rule is None:
            raise KeyError(f"未注册的资源: {request.resource_id}")

        at = request.requested_at
        require_aware(at, "requested_at")
        visible = self._catalog.visible_records(request.subject_id, at)
        view = resolve_view(visible, at)

        if rule.sensitive:
            evaluation = self._evaluate_sensitive(rule, view, at)
        else:
            if not request.business_line:
                raise ValueError("普通请求必须提供 business_line")
            scope = self._business_scopes.get(request.business_line)
            if scope is None:
                raise KeyError(f"未注册的业务范围: {request.business_line}")
            evaluation = self._evaluate_normal(scope, view, at)

        decision = Decision(
            request_id=request.request_id,
            subject_id=request.subject_id,
            resource_id=request.resource_id,
            business_line=request.business_line,
            sensitive=rule.sensitive,
            outcome=evaluation.outcome,
            reasons=evaluation.reasons,
            decided_at=at,
            applicable_record_id=evaluation.applicable_record_id,
            visible_record_ids=tuple(r.record_id for r in visible),
            visible_inputs=visible,
            input_fingerprint=_fingerprint(visible),
        )
        self._ledger[request.request_id] = decision
        return decision

    def _evaluate_sensitive(
        self,
        rule: ResourceRule,
        view: ViewResolution,
        at: datetime,
    ) -> _Evaluation:
        if view.empty:
            return _Evaluation(
                Outcome.MANUAL_REVIEW,
                ("no_visible_directory_record",),
                None,
                False,
                False,
            )
        if view.conflict:
            return _Evaluation(
                Outcome.MANUAL_REVIEW,
                ("conflicting_directory_records",),
                None,
                True,
                False,
            )

        rep = view.representative
        stale = (at - rep.effective_at) > self._freshness_threshold
        reasons: list[str] = []
        if rule.allowed_departments is not None and rep.department not in rule.allowed_departments:
            reasons.append("department_not_allowed")
        if not rep.on_duty:
            reasons.append("subject_not_on_duty")
        if stale:
            reasons.append("stale_directory_record")

        if reasons:
            return _Evaluation(Outcome.MANUAL_REVIEW, tuple(reasons), rep.record_id, False, stale)
        return _Evaluation(Outcome.ALLOW, (), rep.record_id, False, stale)

    def _evaluate_normal(
        self,
        scope: BusinessScope,
        view: ViewResolution,
        at: datetime,
    ) -> _Evaluation:
        if view.empty:
            return _Evaluation(
                Outcome.DENY,
                ("no_visible_directory_record",),
                None,
                False,
                False,
            )
        if view.conflict:
            return _Evaluation(
                Outcome.DENY,
                ("conflicting_directory_records",),
                None,
                True,
                False,
            )

        rep = view.representative
        stale = (at - rep.effective_at) > self._freshness_threshold
        allowed = rep.department in scope.allowed_departments
        if not allowed:
            return _Evaluation(
                Outcome.DENY,
                ("department_outside_business_scope",),
                rep.record_id,
                False,
                stale,
            )
        if scope.require_on_duty and not rep.on_duty:
            return _Evaluation(
                Outcome.DENY,
                ("subject_not_on_duty",),
                rep.record_id,
                False,
                stale,
            )

        reasons = ("stale_directory_record_advisory",) if stale else ()
        return _Evaluation(Outcome.ALLOW, reasons, rep.record_id, False, stale)

    # ------------------------------------------------------------------
    # 既往决定复核
    # ------------------------------------------------------------------
    def review(self, request_id: str, reviewed_at: datetime) -> ReviewReport:
        require_aware(reviewed_at, "reviewed_at")
        stored = self._ledger[request_id]
        rule = self._resource_rules[stored.resource_id]
        # as-of 复核语义：
        # - 可见性按复核时刻（reviewed_at 前已接收的记录，含迟到更正）；
        # - 适用性与新鲜度按原决定时刻（decided_at）评估，因为被复核的是
        #   当时的决定；否则任何历史记录在复核时都会显得过期。
        current_visible = self._catalog.visible_records(stored.subject_id, reviewed_at)
        current_view = resolve_view(current_visible, stored.decided_at)

        if rule.sensitive:
            current_eval = self._evaluate_sensitive(rule, current_view, stored.decided_at)
        else:
            if stored.business_line is None:
                raise ValueError("普通请求缺少 business_line，无法复核")
            scope = self._business_scopes[stored.business_line]
            current_eval = self._evaluate_normal(scope, current_view, stored.decided_at)

        current_fingerprint = _fingerprint(current_visible)
        visible_inputs_changed = current_fingerprint != stored.input_fingerprint
        applicable_data_changed = current_eval.applicable_record_id != stored.applicable_record_id

        reasons: list[str] = list(current_eval.reasons)
        if visible_inputs_changed:
            reasons.append("visible_inputs_changed_since_decision")
        if applicable_data_changed:
            reasons.append("applicable_record_changed_since_decision")

        review_outcome = self._adjudicate(stored.outcome, current_eval.outcome)
        return ReviewReport(
            request_id=request_id,
            reviewed_at=reviewed_at,
            original_outcome=stored.outcome,
            current_outcome=current_eval.outcome,
            review_outcome=review_outcome,
            visible_inputs_changed=visible_inputs_changed,
            applicable_data_changed=applicable_data_changed,
            conflict=current_eval.conflict,
            stale=current_eval.stale,
            reasons=tuple(reasons),
        )

    @staticmethod
    def _adjudicate(original: Outcome, current: Outcome) -> ReviewOutcome:
        if original == Outcome.MANUAL_REVIEW and current in (Outcome.ALLOW, Outcome.DENY):
            return ReviewOutcome.REINFORCED
        if current != original:
            return ReviewOutcome.OVERTURNED
        return ReviewOutcome.UPHELD
