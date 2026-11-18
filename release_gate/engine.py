"""发布判定引擎。

规则汇总：

1. 额度按 ``(环境, 控制项)`` 在考核窗口内累计；同一控制项拆成多次申请，
   消耗的是同一条额度。
2. 已批准豁免的“真实开放区间”由生效区间与实际 OPEN/CLOSE 事件共同决定：
   未到期但已 CLOSE 的豁免在实际关闭时结束；仍处于打开状态的豁免截至
   考核时点 ``as_of`` 或窗口结束。
3. 真实区间先裁剪到考核窗口，再做并集合并——重叠豁免只算一次。
4. 新申请的预计开放区间并入当期已用区间后，累计开放时长超过额度即
   阻止打开，并给出导致超量的具体时间段。
5. ``as_of`` 让复核可以停在任意历史时点：该时点之后的事件不参与计算，
   从而支持事后更正、复核过去的决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from .intervals import (
    Interval,
    clip,
    format_duration,
    intersect,
    merge,
    tail_after_budget,
    to_iso,
    union_duration,
)
from .models import (
    CLOSE,
    OPEN,
    ControlQuota,
    EvaluationBundle,
    Exemption,
    GateEvent,
    NewApplication,
)

REASON_OUT_OF_SCOPE = "ENV_OUT_OF_SCOPE"
REASON_QUOTA_MISSING = "QUOTA_NOT_CONFIGURED"
REASON_QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
REASON_PROJECTION_EMPTY = "PROJECTION_EMPTY"


@dataclass
class Decision:
    """单条新申请的发布判定结果。"""

    application_id: str
    env_id: str
    control_id: str
    allowed: bool
    reason: Optional[str]
    quota: Optional[timedelta]
    used_duration: timedelta
    projected_duration: timedelta
    projected_inside_window: list[Interval]
    used_intervals: list[Interval]
    combined_intervals: list[Interval]
    combined_duration: timedelta
    overage_segments: list[Interval]
    warnings: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return not self.allowed

    def to_dict(self) -> dict:
        return {
            "application_id": self.application_id,
            "env_id": self.env_id,
            "control_id": self.control_id,
            "allowed": self.allowed,
            "reason": self.reason,
            "quota": format_duration(self.quota) if self.quota is not None else None,
            "used_duration": format_duration(self.used_duration),
            "projected_duration": format_duration(self.projected_duration),
            "projected_inside_window": [iv.to_dict() for iv in self.projected_inside_window],
            "used_intervals": [iv.to_dict() for iv in self.used_intervals],
            "combined_intervals": [iv.to_dict() for iv in self.combined_intervals],
            "combined_duration": format_duration(self.combined_duration),
            "overage_segments": [iv.to_dict() for iv in self.overage_segments],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Decision":
        return cls(
            application_id=data["application_id"],
            env_id=data["env_id"],
            control_id=data["control_id"],
            allowed=data["allowed"],
            reason=data.get("reason"),
            quota=None,
            used_duration=timedelta(0),
            projected_duration=timedelta(0),
            projected_inside_window=[Interval.from_dict(x) for x in data.get("projected_inside_window", [])],
            used_intervals=[Interval.from_dict(x) for x in data.get("used_intervals", [])],
            combined_intervals=[Interval.from_dict(x) for x in data.get("combined_intervals", [])],
            combined_duration=timedelta(0),
            overage_segments=[Interval.from_dict(x) for x in data.get("overage_segments", [])],
            warnings=list(data.get("warnings", [])),
        )


@dataclass
class BundleEvaluation:
    """整包输入的判定结果与可复核快照要素。"""

    window: Interval
    as_of: Optional[object]
    decisions: list[Decision]
    open_intervals_by_key: dict[tuple[str, str], list[Interval]]
    warnings_by_key: dict[tuple[str, str], list[str]]

    @property
    def all_allowed(self) -> bool:
        return all(d.allowed for d in self.decisions)

    def to_dict(self) -> dict:
        return {
            "window": self.window.to_dict(),
            "as_of": to_iso(self.as_of) if self.as_of else None,
            "all_allowed": self.all_allowed,
            "open_intervals": [
                {
                    "env_id": key[0],
                    "control_id": key[1],
                    "intervals": [iv.to_dict() for iv in ivs],
                    "warnings": list(self.warnings_by_key.get(key, [])),
                }
                for key, ivs in sorted(self.open_intervals_by_key.items())
            ],
            "decisions": [d.to_dict() for d in self.decisions],
        }


def real_open_segments(
    exemption: Exemption,
    events: list[GateEvent],
    window: Interval,
    as_of,
) -> tuple[list[Interval], list[str]]:
    """根据生效区间和真实启停事件，推导该豁免在窗口内的真实开放区间。

    配对规则：按时间排序后取 OPEN 与随后最近的 CLOSE 配对；OPEN 之后没有
    CLOSE 则开放到 ``min(生效结束, as_of, 窗口结束)``。窗口外、``as_of``
    之后的事件一律忽略。事件与生效区间不一致时产生告警（防御性检测，
    不静默吞掉异常数据）。
    """
    warnings: list[str] = []
    eff = clip(
        Interval(exemption.effective_start, exemption.effective_end),
        window,
    )
    if eff is None:
        return [], [f"豁免 {exemption.exemption_id} 生效区间完全落在考核窗口之外"]

    horizon = min(eff.end, as_of) if as_of is not None else eff.end
    if horizon <= eff.start:
        return [], [
            f"豁免 {exemption.exemption_id} 在考核时点 {to_iso(as_of)} 尚未生效"
        ]

    related = sorted(
        (
            ev
            for ev in events
            if ev.exemption_id == exemption.exemption_id
            and ev.env_id == exemption.env_id
            and ev.control_id == exemption.control_id
            and ev.occurred_at <= horizon
        ),
        key=lambda ev: (ev.occurred_at, ev.event_id),
    )

    segments: list[Interval] = []
    open_since = None
    for ev in related:
        if ev.action == OPEN:
            if open_since is not None:
                warnings.append(
                    f"豁免 {exemption.exemption_id} 在 {to_iso(ev.occurred_at)} "
                    "出现重复 OPEN，已忽略"
                )
                continue
            open_since = ev.occurred_at
            if open_since < exemption.effective_start:
                warnings.append(
                    f"豁免 {exemption.exemption_id} 的 OPEN 早于生效开始时间，"
                    "开放起点按生效开始时间计算"
                )
                open_since = exemption.effective_start
        else:  # CLOSE
            if open_since is None:
                warnings.append(
                    f"豁免 {exemption.exemption_id} 在 {to_iso(ev.occurred_at)} "
                    "出现无匹配 OPEN 的 CLOSE，已忽略"
                )
                continue
            close_at = ev.occurred_at
            if close_at > exemption.effective_end:
                warnings.append(
                    f"豁免 {exemption.exemption_id} 的 CLOSE 晚于生效结束时间，"
                    "开放终点按生效结束时间计算"
                )
                close_at = exemption.effective_end
            if close_at > open_since:
                segments.append(Interval(open_since, close_at))
            open_since = None

    if open_since is not None:
        # 尚未到期也未收回：截至考核时点；到期未关：截至生效结束。
        segments.append(Interval(open_since, horizon))

    clipped: list[Interval] = []
    for seg in segments:
        inside = clip(seg, window)
        if inside is not None:
            clipped.append(inside)
    return merge(clipped), warnings


def used_open_intervals(
    bundle: EvaluationBundle,
) -> tuple[dict[tuple[str, str], list[Interval]], dict[tuple[str, str], list[str]]]:
    """汇总所有已批准豁免在窗口内的真实开放区间（按额度键合并并集）。"""
    by_key: dict[tuple[str, str], list[Interval]] = {}
    warnings_by_key: dict[tuple[str, str], list[str]] = {}
    for exemption in bundle.exemptions:
        key = (exemption.env_id, exemption.control_id)
        segments, warnings = real_open_segments(
            exemption, bundle.events, bundle.window, bundle.as_of
        )
        by_key.setdefault(key, []).extend(segments)
        warnings_by_key.setdefault(key, []).extend(warnings)
    merged = {key: merge(ivs) for key, ivs in by_key.items()}
    return merged, warnings_by_key


def _find_quota(
    quotas: list[ControlQuota], env_id: str, control_id: str
) -> Optional[ControlQuota]:
    for quota in quotas:
        if quota.env_id == env_id and quota.control_id == control_id:
            return quota
    return None


def evaluate_application(
    application: NewApplication,
    used_intervals: list[Interval],
    quota: Optional[ControlQuota],
    window: Interval,
    environments: list[str],
) -> Decision:
    """判定单条新申请：把预计开放区间并入当期已用区间后检查额度。"""
    warnings: list[str] = []
    projected = merge(
        iv for iv in (clip(iv, window) for iv in application.projected_intervals)
        if iv is not None
    )
    projected_duration = union_duration(projected)

    used_intervals = merge(used_intervals)
    used_duration = union_duration(used_intervals)
    combined = merge(used_intervals + projected)
    combined_duration = union_duration(combined)

    base = Decision(
        application_id=application.application_id,
        env_id=application.env_id,
        control_id=application.control_id,
        allowed=True,
        reason=None,
        quota=quota.max_open_duration if quota else None,
        used_duration=used_duration,
        projected_duration=projected_duration,
        projected_inside_window=projected,
        used_intervals=used_intervals,
        combined_intervals=combined,
        combined_duration=combined_duration,
        overage_segments=[],
        warnings=warnings,
    )

    if application.env_id not in environments:
        base.allowed = False
        base.reason = REASON_OUT_OF_SCOPE
        base.warnings.append(
            f"环境 {application.env_id} 不在考核窗口的环境范围内，默认阻止打开"
        )
        return base

    if quota is None:
        base.allowed = False
        base.reason = REASON_QUOTA_MISSING
        base.warnings.append(
            f"环境 {application.env_id} 控制项 {application.control_id} "
            "未配置累计开放额度，默认阻止打开"
        )
        return base

    if not projected:
        base.allowed = False
        base.reason = REASON_PROJECTION_EMPTY
        base.warnings.append("预计开放区间裁剪到考核窗口后为空")
        return base

    if combined_duration > quota.max_open_duration:
        # 超量时间段：跳过已消耗额度后，预计开放区间中超出的部分。
        overage = tail_after_budget(
            intersect(projected, combined),
            used=used_duration,
            budget=quota.max_open_duration,
        )
        base.allowed = False
        base.reason = REASON_QUOTA_EXCEEDED
        base.overage_segments = overage
        base.warnings.append(
            "累计开放 "
            f"{format_duration(combined_duration)} 超过额度 "
            f"{format_duration(quota.max_open_duration)}，"
            f"超量 {format_duration(combined_duration - quota.max_open_duration)}"
        )
    return base


def evaluate_bundle(bundle: EvaluationBundle) -> BundleEvaluation:
    """对整包输入执行发布判定。

    同一批次内的申请按输入顺序依次判定：前一份申请被放行后，其预计开放
    区间立即并入该 ``(环境, 控制项)`` 的当期已用区间，供后续申请扣减——
    这正是“同一控制项拆成多次申请也消耗同一额度”的批次内体现。被阻止
    的申请不占用额度，其区间不会并入已用区间。
    """
    open_by_key, warnings_by_key = used_open_intervals(bundle)
    decisions: list[Decision] = []
    for application in bundle.applications:
        key = (application.env_id, application.control_id)
        quota = _find_quota(bundle.quotas, application.env_id, application.control_id)
        decision = evaluate_application(
            application,
            open_by_key.get(key, []),
            quota,
            bundle.window,
            bundle.environments,
        )
        decision.warnings.extend(warnings_by_key.get(key, []))
        decisions.append(decision)
        if decision.allowed:
            open_by_key.setdefault(key, []).extend(decision.projected_inside_window)
            open_by_key[key] = merge(open_by_key[key])
    return BundleEvaluation(
        window=bundle.window,
        as_of=bundle.as_of,
        decisions=decisions,
        open_intervals_by_key=open_by_key,
        warnings_by_key=warnings_by_key,
    )
