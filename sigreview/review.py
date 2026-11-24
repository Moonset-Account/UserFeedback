"""离线回归审查器。

审查规则严格对应题面：

1. 轨迹按 **接口 + 签名数据摘要** 分组；同组内签名值都正确、仅证书
   用途不同的正反例才具备配对资格。
2. 正例 = 严格策略用途检查通过且最终放行；负例按最终结局分类：

   * :data:`NegativeClass.PURPOSE_ENFORCED_DENY`：严格策略以用途不符拒绝，
     且最终结论仍为拒绝——只有这类负例能给用途限制“记功”；
   * :data:`NegativeClass.COMPATIBILITY_FALLBACK_ALLOW`：严格策略用途拒绝，
     兼容策略随后放行——单独列为缺口，不能记功；
   * :data:`NegativeClass.BAD_SIGNATURE_DENY`：签名值错误提前拒绝，
     根本没进入用途检查，不能记功。

3. 同组正例与“用途强制拒绝”负例同时存在，该接口的这项用途限制才算
   被覆盖；任一观测缺失都在证据对中明确标出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence

from .verifier import (
    CheckResult,
    Decision,
    RejectReason,
    Trace,
)


class MissingObservation(str, Enum):
    """证据对中缺失的观测类型。"""

    MISSING_POSITIVE = "missing_positive_allow"
    MISSING_NEGATIVE_PURPOSE_DENY = "missing_negative_purpose_deny"

    @property
    def label(self) -> str:
        return _MISSING_LABELS[self]


_MISSING_LABELS = {
    MissingObservation.MISSING_POSITIVE: "用途匹配并放行的正例",
    MissingObservation.MISSING_NEGATIVE_PURPOSE_DENY:
        "最终因用途检查而拒绝的负例",
}


class NegativeClass(str, Enum):
    """签名正确的负例（或被排除的签名错误请求）的归因类别。"""

    PURPOSE_ENFORCED_DENY = "purpose_enforced_deny"
    COMPATIBILITY_FALLBACK_ALLOW = "compatibility_fallback_allow"
    BAD_SIGNATURE_DENY = "bad_signature_deny"
    OTHER_DENY = "other_deny"

    @property
    def label(self) -> str:
        return _NEGATIVE_LABELS[self]


_NEGATIVE_LABELS = {
    NegativeClass.PURPOSE_ENFORCED_DENY: "用途不符被强制拒绝",
    NegativeClass.COMPATIBILITY_FALLBACK_ALLOW: "严格拒绝后兼容放行",
    NegativeClass.BAD_SIGNATURE_DENY: "签名错误提前拒绝",
    NegativeClass.OTHER_DENY: "其它原因拒绝",
}


# ---------------------------------------------------------------------------
# 报告数据结构
# ---------------------------------------------------------------------------


@dataclass
class EvidencePair:
    """同一接口、同一数据摘要下的一对观测（可能缺正例或缺负例）。"""

    interface: str
    data_digest: str
    expected_purpose: str
    positive_trace: Optional[Trace] = None
    negative_trace: Optional[Trace] = None
    negative_class: Optional[NegativeClass] = None
    missing: List[MissingObservation] = field(default_factory=list)
    credited: bool = False
    rationale: str = ""

    @property
    def is_fallback_hole(self) -> bool:
        return self.negative_class is NegativeClass.COMPATIBILITY_FALLBACK_ALLOW


@dataclass
class InterfaceReport:
    """单个接口的审查结论。"""

    interface: str
    expected_purpose: str
    evidence_pairs: List[EvidencePair] = field(default_factory=list)
    bad_signature_traces: List[Trace] = field(default_factory=list)

    @property
    def covered(self) -> bool:
        return any(pair.credited for pair in self.evidence_pairs)

    @property
    def fallback_holes(self) -> List[EvidencePair]:
        return [pair for pair in self.evidence_pairs if pair.is_fallback_hole]

    @property
    def missing_observations(self) -> List[str]:
        notes: List[str] = []
        for pair in self.evidence_pairs:
            for item in pair.missing:
                notes.append(
                    f"摘要 {pair.data_digest[:12]}… 缺少{item.label}"
                )
        return notes


@dataclass
class ReviewReport:
    """整批轨迹的审查报告。"""

    interface_reports: List[InterfaceReport] = field(default_factory=list)
    total_traces: int = 0

    @property
    def covered_interfaces(self) -> List[str]:
        return [r.interface for r in self.interface_reports if r.covered]

    @property
    def uncovered_interfaces(self) -> List[str]:
        return [r.interface for r in self.interface_reports if not r.covered]

    @property
    def all_fallback_holes(self) -> List[EvidencePair]:
        holes: List[EvidencePair] = []
        for report in self.interface_reports:
            holes.extend(report.fallback_holes)
        return holes

    def to_dict(self) -> dict:
        return {
            "total_traces": self.total_traces,
            "covered_interfaces": self.covered_interfaces,
            "uncovered_interfaces": self.uncovered_interfaces,
            "fallback_hole_count": len(self.all_fallback_holes),
            "interfaces": [
                {
                    "interface": report.interface,
                    "expected_purpose": report.expected_purpose,
                    "covered": report.covered,
                    "missing_observations": report.missing_observations,
                    "bad_signature_requests": [
                        t.request_id for t in report.bad_signature_traces
                    ],
                    "evidence_pairs": [
                        {
                            "data_digest": pair.data_digest,
                            "expected_purpose": pair.expected_purpose,
                            "positive_request": (
                                pair.positive_trace.request_id
                                if pair.positive_trace
                                else None
                            ),
                            "negative_request": (
                                pair.negative_trace.request_id
                                if pair.negative_trace
                                else None
                            ),
                            "negative_class": (
                                pair.negative_class.value
                                if pair.negative_class
                                else None
                            ),
                            "credited": pair.credited,
                            "missing": [m.value for m in pair.missing],
                            "rationale": pair.rationale,
                            "positive_trace": (
                                pair.positive_trace.to_dict()
                                if pair.positive_trace
                                else None
                            ),
                            "negative_trace": (
                                pair.negative_trace.to_dict()
                                if pair.negative_trace
                                else None
                            ),
                        }
                        for pair in report.evidence_pairs
                    ],
                }
                for report in self.interface_reports
            ],
        }

    def render_text(self) -> str:
        """渲染为中文纯文本报告。"""
        lines: List[str] = []
        lines.append("证书用途限制离线回归审查报告")
        lines.append("=" * 60)
        lines.append(f"轨迹总数：{self.total_traces}")
        lines.append(
            f"用途限制已覆盖接口（{len(self.covered_interfaces)}）："
            + (", ".join(self.covered_interfaces) or "无")
        )
        lines.append(
            f"未覆盖接口（{len(self.uncovered_interfaces)}）："
            + (", ".join(self.uncovered_interfaces) or "无")
        )
        lines.append(
            f"严格拒绝后兼容放行缺口数：{len(self.all_fallback_holes)}"
        )
        lines.append("")

        for report in self.interface_reports:
            lines.append(f"接口 {report.interface}")
            lines.append("-" * 60)
            lines.append(f"  期望证书用途：{report.expected_purpose}")
            lines.append(
                "  覆盖结论："
                + ("已覆盖（存在记功证据对）" if report.covered else "未覆盖")
            )
            if report.missing_observations:
                lines.append("  证据不足：")
                for note in report.missing_observations:
                    lines.append(f"    - {note}")
            for pair in report.evidence_pairs:
                lines.append("  证据对：")
                lines.append(f"    数据摘要：{pair.data_digest}")
                if pair.positive_trace is not None:
                    t = pair.positive_trace
                    lines.append(
                        f"    正例：{t.request_id} 最终={t.final_decision.value}；"
                        f"尝试策略={_strategies_label(t)}"
                    )
                else:
                    lines.append("    正例：缺失（无用途匹配且放行的观测）")
                if pair.negative_trace is not None:
                    t = pair.negative_trace
                    lines.append(
                        f"    负例：{t.request_id} 类别="
                        f"{pair.negative_class.label}（{pair.negative_class.value}） 最终="
                        f"{t.final_decision.value}；"
                        f"尝试策略={_strategies_label(t)}"
                    )
                    if pair.is_fallback_hole:
                        lines.append(
                            "      备注：严格策略已因用途不符拒绝，"
                            "兼容策略跳过用途检查后放行——限制未真正生效"
                        )
                else:
                    lines.append("    负例：缺失（无用途不符的观测）")
                lines.append(
                    "    记功：" + ("是" if pair.credited else "否")
                    + f"；判定依据：{pair.rationale}"
                )
            if report.bad_signature_traces:
                lines.append("  签名错误提前拒绝（不参与用途配对）：")
                for t in report.bad_signature_traces:
                    lines.append(
                        f"    - {t.request_id} 摘要={t.data_digest[:12]}… "
                        f"原因={t.final_reason.value if t.final_reason else 'n/a'}"
                    )
            lines.append("")

        if self.all_fallback_holes:
            lines.append("严格拒绝 / 兼容放行缺口清单")
            lines.append("-" * 60)
            for pair in self.all_fallback_holes:
                lines.append(
                    f"- 接口 {pair.interface} 摘要 {pair.data_digest[:12]}… "
                    f"负例请求 {pair.negative_trace.request_id}"
                )
            lines.append("")
        return "\n".join(lines)


def _strategies_label(trace: Trace) -> str:
    parts = []
    for attempt in trace.attempts:
        result = attempt.decision.value
        reason = f"/{attempt.reason.value}" if attempt.reason else ""
        parts.append(f"{attempt.strategy.value}->{result}{reason}")
    return " -> ".join(parts)


# ---------------------------------------------------------------------------
# 审查器
# ---------------------------------------------------------------------------


class RegressionReviewer:
    """对验签轨迹做证据配对与用途限制覆盖判定。"""

    def review(self, traces: Sequence[Trace]) -> ReviewReport:
        by_interface: Dict[str, List[Trace]] = {}
        for trace in traces:
            by_interface.setdefault(trace.interface, []).append(trace)

        reports: List[InterfaceReport] = []
        for interface in sorted(by_interface):
            reports.append(self._review_interface(interface, by_interface[interface]))

        return ReviewReport(interface_reports=reports, total_traces=len(traces))

    # ---- 单接口审查 -----------------------------------------------------

    def _review_interface(
        self, interface: str, traces: Sequence[Trace]
    ) -> InterfaceReport:
        expected_purposes = {t.expected_purpose for t in traces}
        if len(expected_purposes) != 1:
            raise ValueError(
                f"接口 {interface} 的轨迹出现多个期望用途："
                f"{sorted(p.value for p in expected_purposes)}"
            )
        expected_purpose = next(iter(expected_purposes))

        sig_valid = [t for t in traces if t.signature_valid]
        sig_bad = [t for t in traces if not t.signature_valid]

        groups: Dict[str, List[Trace]] = {}
        for trace in sig_valid:
            groups.setdefault(trace.data_digest, []).append(trace)

        pairs: List[EvidencePair] = []
        for digest in sorted(groups):
            pairs.append(self._build_pair(interface, digest, expected_purpose.value,
                                          groups[digest]))

        return InterfaceReport(
            interface=interface,
            expected_purpose=expected_purpose.value,
            evidence_pairs=pairs,
            bad_signature_traces=sorted(sig_bad, key=lambda t: t.request_id),
        )

    def _build_pair(
        self,
        interface: str,
        digest: str,
        expected_purpose: str,
        group: Sequence[Trace],
    ) -> EvidencePair:
        positives = [t for t in group if self._is_positive(t)]
        enforced = [
            t for t in group
            if self._classify_negative(t) is NegativeClass.PURPOSE_ENFORCED_DENY
        ]
        fallbacks = [
            t for t in group
            if self._classify_negative(t)
            is NegativeClass.COMPATIBILITY_FALLBACK_ALLOW
        ]

        positive = positives[0] if positives else None
        # 优先选取最终仍被拒绝的用途负例；否则保留兼容放行缺口作为负例观测。
        if enforced:
            negative = enforced[0]
            negative_class = NegativeClass.PURPOSE_ENFORCED_DENY
        elif fallbacks:
            negative = fallbacks[0]
            negative_class = NegativeClass.COMPATIBILITY_FALLBACK_ALLOW
        else:
            negative = None
            negative_class = None

        missing: List[MissingObservation] = []
        if positive is None:
            missing.append(MissingObservation.MISSING_POSITIVE)
        # 兼容放行负例不能替代“最终因用途检查而拒绝”的观测
        if not enforced:
            missing.append(
                MissingObservation.MISSING_NEGATIVE_PURPOSE_DENY
            )

        credited = positive is not None and bool(enforced)
        if credited:
            rationale = (
                "同摘要下正例用途匹配并放行，负例签名正确但因用途不符被"
                "严格策略拒绝且最终未放行，用途限制生效"
            )
        elif positive is not None and fallbacks and not enforced:
            rationale = (
                "负例被严格策略拒绝后由兼容策略放行，用途限制未闭环；"
                "缺少最终因用途检查而拒绝的负例观测"
            )
        elif positive is None and (enforced or fallbacks):
            rationale = "存在用途不符负例，但缺少同摘要的用途匹配正例"
        elif positive is not None:
            rationale = "存在正例，但同摘要下没有任何用途不符的负例观测"
        else:
            rationale = "同摘要下既无正例也无有效用途负例，无法构成证据对"

        return EvidencePair(
            interface=interface,
            data_digest=digest,
            expected_purpose=expected_purpose,
            positive_trace=positive,
            negative_trace=negative,
            negative_class=negative_class,
            missing=missing,
            credited=credited,
            rationale=rationale,
        )

    # ---- 轨迹归因 -------------------------------------------------------

    @staticmethod
    def _is_positive(trace: Trace) -> bool:
        return (
            trace.strict_purpose_check is CheckResult.PURPOSE_OK
            and trace.final_decision is Decision.ALLOW
        )

    @staticmethod
    def _classify_negative(trace: Trace) -> NegativeClass:
        strict_check = trace.strict_purpose_check
        if strict_check is not CheckResult.PURPOSE_MISMATCH:
            # 严格策略未做出用途不符判定（含未进入用途检查的情形）。
            return NegativeClass.OTHER_DENY
        if trace.final_decision is Decision.ALLOW:
            return NegativeClass.COMPATIBILITY_FALLBACK_ALLOW
        if trace.final_reason is RejectReason.PURPOSE_MISMATCH:
            return NegativeClass.PURPOSE_ENFORCED_DENY
        return NegativeClass.OTHER_DENY
