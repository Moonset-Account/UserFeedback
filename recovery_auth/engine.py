"""恢复授权判定引擎。

判定规则（对应题面）：
1. 确认归属以发生时的渠道版本为准：邮件/设备确认按其 occurred_at 落在
   哪个渠道版本区间内归属，且确认的渠道值必须等于该版本的绑定值。
2. 高风险许可：发起（occurred_at）之前必须有两条来自不同恢复来源
   （email / device / service）的有效证明；普通风险至少一条有效证明。
3. 较晚入账的取消可以推翻仍待生效的许可：取消的 occurred_at 早于许可
   生效时刻（effective_from）时，许可整体无效；发生在有效期内的取消
   将有效区间截断。取消只需在复算入账点可见（recorded_at <= as_of）。
4. 对已执行的决定附更正和当时可见的依据：存在 Execution 记录时，按
   执行时刻的入账点复算当时依据，与当前结论一并输出。
5. 支持按任一历史入账点复算：evaluate(..., as_of=入账时刻)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .events import (
    Cancellation,
    ChannelRebind,
    DeviceConfirmation,
    EmailConfirmation,
    Event,
    Execution,
    RecoveryRequest,
    ServiceCredential,
)
from .ledger import Ledger
from .result import (
    ChannelVersion,
    Correction,
    Decision,
    DecisionSummary,
    EvidenceChain,
    EvidenceLink,
    Interval,
    RebindCrossing,
)

SOURCE_EMAIL = "email"
SOURCE_DEVICE = "device"
SOURCE_SERVICE = "service"


@dataclass(frozen=True)
class EngineConfig:
    """判定参数。

    pending_period: 许可从发起到生效的待生效期（仍待生效的许可可被取消推翻）。
    validity_period: 许可生效后的有效时长。
    """

    pending_period: timedelta = timedelta(hours=24)
    validity_period: timedelta = timedelta(days=7)
    min_proofs_normal: int = 1
    min_distinct_sources_high: int = 2


class AuthorizationEngine:
    """恢复授权判定引擎。"""

    def __init__(self, ledger: Ledger, config: Optional[EngineConfig] = None) -> None:
        self.ledger = ledger
        self.config = config or EngineConfig()

    def evaluate(self, request_id: str, as_of: Optional[datetime] = None) -> Decision:
        """按入账点 as_of 复算 request_id 的许可。as_of=None 表示当前全部可见。"""
        return self._decide(request_id, as_of, with_correction=True)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _decide(self, request_id: str, as_of: Optional[datetime], with_correction: bool) -> Decision:
        events = self.ledger.visible(as_of)
        request = next(
            (e for e in events if isinstance(e, RecoveryRequest) and e.event_id == request_id),
            None,
        )
        if request is None:
            raise LookupError(f"request {request_id!r} 在入账点 {as_of} 不可见")

        t0 = request.occurred_at  # 发起时刻
        rebinds = [e for e in events if isinstance(e, ChannelRebind)]
        versions = self._build_versions(rebinds)

        proofs = [
            e
            for e in events
            if isinstance(e, (EmailConfirmation, DeviceConfirmation, ServiceCredential))
        ]
        links = tuple(self._link(p, t0, versions) for p in proofs)
        accepted = [l for l in links if l.accepted]
        sources = {l.source for l in accepted}

        if request.risk_level == "high":
            granted = len(sources) >= self.config.min_distinct_sources_high
        else:
            granted = len(accepted) >= self.config.min_proofs_normal

        effective_from = t0 + self.config.pending_period
        expires_at = effective_from + self.config.validity_period

        # 取消：推翻仍待生效的许可，或截断有效期内的许可
        revoked = False
        interval: Optional[Interval] = None
        if granted:
            end = expires_at
            cancels = sorted(
                (
                    c
                    for c in events
                    if isinstance(c, Cancellation) and c.target_request_id == request_id
                ),
                key=lambda c: (c.occurred_at, c.recorded_at, c.event_id),
            )
            if cancels and cancels[0].occurred_at < expires_at:
                revoked = True
                end = cancels[0].occurred_at
            if end > effective_from:
                interval = Interval(effective_from, end)

        chain = EvidenceChain(
            links=links,
            crossings=self._crossings(rebinds, accepted, t0, versions),
            distinct_sources=frozenset(sources),
        )

        correction = None
        if with_correction:
            correction = self._correction(
                request_id, events, granted, revoked, interval, sources
            )

        return Decision(
            request_id=request_id,
            as_of=as_of,
            granted=granted,
            revoked=revoked,
            validity_interval=interval,
            evidence_chain=chain,
            correction=correction,
        )

    @staticmethod
    def _build_versions(rebinds: List[ChannelRebind]) -> Dict[str, List[ChannelVersion]]:
        """按渠道类型从改绑事件构建版本序列（按发生时刻排序）。

        同一发生时刻的多次改绑，较晚入账者排在后，先前者区间退化为空。
        """
        by_kind: Dict[str, List[ChannelRebind]] = {}
        for r in sorted(rebinds, key=lambda e: (e.occurred_at, e.recorded_at, e.event_id)):
            by_kind.setdefault(r.channel_kind, []).append(r)
        versions: Dict[str, List[ChannelVersion]] = {}
        for kind, rs in by_kind.items():
            vs = []
            for i, r in enumerate(rs):
                end = rs[i + 1].occurred_at if i + 1 < len(rs) else None
                vs.append(
                    ChannelVersion(
                        channel_kind=kind,
                        version=i + 1,
                        value=r.new_value,
                        start=r.occurred_at,
                        end=end,
                        rebind_event_id=r.event_id,
                    )
                )
            versions[kind] = vs
        return versions

    @staticmethod
    def _attribute(versions: List[ChannelVersion], t: datetime) -> Optional[ChannelVersion]:
        """确认归属：以发生时的渠道版本为准。"""
        for v in versions:
            if v.covers(t):
                return v
        return None

    def _link(
        self, proof: Event, t0: datetime, versions: Dict[str, List[ChannelVersion]]
    ) -> EvidenceLink:
        source = self._source_of(proof)
        if not proof.occurred_at < t0:
            return EvidenceLink(
                proof.event_id, source, proof.occurred_at, None, False,
                "证明不早于发起时刻，不计入",
            )
        if isinstance(proof, EmailConfirmation):
            if not proof.verified:
                return EvidenceLink(
                    proof.event_id, source, proof.occurred_at, None, False,
                    "邮件确认未验真",
                )
            v = self._attribute(versions.get(SOURCE_EMAIL, []), proof.occurred_at)
            if v is None:
                return EvidenceLink(
                    proof.event_id, source, proof.occurred_at, None, False,
                    "发生时邮件渠道无有效版本",
                )
            if v.value != proof.channel_value:
                return EvidenceLink(
                    proof.event_id, source, proof.occurred_at, v, False,
                    f"确认渠道与发生时的渠道版本 v{v.version} 绑定值不一致",
                )
            return EvidenceLink(
                proof.event_id, source, proof.occurred_at, v, True,
                f"归属于发生时的渠道版本 v{v.version}",
            )
        if isinstance(proof, DeviceConfirmation):
            v = self._attribute(versions.get(SOURCE_DEVICE, []), proof.occurred_at)
            if v is None:
                return EvidenceLink(
                    proof.event_id, source, proof.occurred_at, None, False,
                    "发生时设备渠道无有效版本",
                )
            if v.value != proof.device_id:
                return EvidenceLink(
                    proof.event_id, source, proof.occurred_at, v, False,
                    f"确认设备与发生时的渠道版本 v{v.version} 绑定值不一致",
                )
            return EvidenceLink(
                proof.event_id, source, proof.occurred_at, v, True,
                f"归属于发生时的渠道版本 v{v.version}",
            )
        # ServiceCredential
        assert isinstance(proof, ServiceCredential)
        if proof.valid_until is not None and t0 > proof.valid_until:
            return EvidenceLink(
                proof.event_id, source, proof.occurred_at, None, False,
                "客服凭据在发起前已过期",
            )
        return EvidenceLink(
            proof.event_id, source, proof.occurred_at, None, True,
            "客服凭据有效",
        )

    @staticmethod
    def _source_of(proof: Event) -> str:
        if isinstance(proof, EmailConfirmation):
            return SOURCE_EMAIL
        if isinstance(proof, DeviceConfirmation):
            return SOURCE_DEVICE
        return SOURCE_SERVICE

    @staticmethod
    def _crossings(
        rebinds: List[ChannelRebind],
        accepted: List[EvidenceLink],
        t0: datetime,
        versions: Dict[str, List[ChannelVersion]],
    ) -> Tuple[RebindCrossing, ...]:
        """跨改绑边界：最早被采纳证明之后、发起之前发生的改绑。"""
        if not accepted:
            return ()
        earliest = min(l.occurred_at for l in accepted)
        crossings = []
        for r in sorted(rebinds, key=lambda e: (e.occurred_at, e.recorded_at, e.event_id)):
            if earliest < r.occurred_at <= t0:
                kind_versions = versions.get(r.channel_kind, [])
                to_version = next(
                    (v.version for v in kind_versions if v.rebind_event_id == r.event_id),
                    None,
                )
                crossings.append(
                    RebindCrossing(
                        rebind_event_id=r.event_id,
                        channel_kind=r.channel_kind,
                        from_version=(to_version - 1) if to_version and to_version > 1 else None,
                        to_version=to_version if to_version is not None else 0,
                        occurred_at=r.occurred_at,
                    )
                )
        return tuple(crossings)

    def _correction(
        self,
        request_id: str,
        events: List[Event],
        granted: bool,
        revoked: bool,
        interval: Optional[Interval],
        sources: set,
    ) -> Optional[Correction]:
        """对已执行的决定：附更正和当时（执行时刻入账点）可见的依据。"""
        executions = [
            e for e in events if isinstance(e, Execution) and e.request_id == request_id
        ]
        if not executions:
            return None
        first_exec = min(executions, key=lambda e: (e.occurred_at, e.recorded_at, e.event_id))
        basis = self._decide(request_id, as_of=first_exec.occurred_at, with_correction=False)
        then = DecisionSummary(
            granted=basis.granted,
            revoked=basis.revoked,
            validity_interval=basis.validity_interval,
            distinct_sources=tuple(sorted(basis.evidence_chain.distinct_sources)),
        )
        now = DecisionSummary(
            granted=granted,
            revoked=revoked,
            validity_interval=interval,
            distinct_sources=tuple(sorted(sources)),
        )
        changed = then != now
        if changed:
            reason = (
                f"执行时可见依据判定为 {self._status_of(then)}，"
                f"按当前入账复算为 {self._status_of(now)}，予以更正"
            )
        else:
            reason = "复算结果与执行时可见依据一致，无需更正"
        return Correction(
            executed_at=first_exec.occurred_at,
            then_visible_basis=then,
            current_finding=now,
            changed=changed,
            reason=reason,
        )

    @staticmethod
    def _status_of(s: DecisionSummary) -> str:
        if not s.granted:
            return "denied"
        if s.revoked:
            return "revoked" if s.validity_interval is None else "truncated"
        return "permitted"
