"""审计核心:对已完成的读取操作做可线性化判定。

判定问题:是否存在一种 "变更生效时刻 + 读取观察时刻" 的安排,使得

1. 每次权限变更在其回执窗口内生效,且同租户内版本非递减;
2. 每次读取在其调用时段 [start, end] 内的 *某一个时刻* 观察授权状态,
   返回字段全部属于该单一时刻状态所允许的字段
   (禁止把两代授权各允许的字段拼到一起);
3. 读取若携带授权版本,则观察时刻的状态版本必须与之相等。

结论:
- CONSISTENT    存在合法安排,附判定依据(见证调度);
- CONFLICT      所有安排均矛盾,定位最小冲突记录集合;
- MANUAL_REVIEW 影响结论的回执缺失,转人工。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from .models import AuditInput, PermissionChange, ReadRecord

# 时间赋值搜索的安全上限(合成记录规模下不会触及)。
_MAX_ASSIGNMENTS = 200_000


class Verdict(str, Enum):
    CONSISTENT = "consistent"
    CONFLICT = "conflict"
    MANUAL_REVIEW = "manual_review"


@dataclass(frozen=True)
class ReadWitness:
    """单次读取的判定依据。"""

    read_id: str
    observed_at: float
    state_version: int
    slot: int  # 在变更序列中的位置:0 表示所有变更生效之前


@dataclass
class AuditResult:
    verdict: Verdict
    # CONSISTENT 时的见证:变更 id -> 生效时刻;读取 id -> ReadWitness
    change_times: Dict[str, float] = field(default_factory=dict)
    read_witnesses: Dict[str, ReadWitness] = field(default_factory=dict)
    # CONFLICT 时的最小冲突读取记录 id 及涉及的变更 id
    conflicting_reads: Tuple[str, ...] = ()
    involved_changes: Tuple[str, ...] = ()
    # MANUAL_REVIEW 时缺失回执且影响结论的变更 id
    pending_receipts: Tuple[str, ...] = ()
    reasons: Tuple[str, ...] = ()


# 状态快照:subject -> resource -> 可见字段集合
Snapshot = Dict[str, Dict[str, FrozenSet[str]]]


class Auditor:
    def __init__(self, audit_input: AuditInput):
        self._input = audit_input
        self._changes = list(audit_input.changes)
        self._reads = list(audit_input.reads)

    # ------------------------------------------------------------------ API

    def run(self) -> AuditResult:
        witness = self._find_witness(self._reads)
        if witness is not None:
            order, times, read_wits = witness
            return self._consistent_result(order, times, read_wits)
        return self._conflict_result()

    # ------------------------------------------------------- 一致性搜索

    def _find_witness(
        self, reads: Sequence[ReadRecord]
    ) -> Optional[Tuple[List[PermissionChange], List[float], Dict[str, ReadWitness]]]:
        """搜索一组 (变更顺序, 生效时刻, 各读取观察点)。找到即返回。"""
        for order in self._version_respecting_orders():
            states = self._states_for_order(order)
            slot_versions = self._slot_versions(order)
            assignment = self._search_times(order, states, slot_versions, reads)
            if assignment is not None:
                times, read_wits = assignment
                return order, times, read_wits
        return None

    def _version_respecting_orders(self) -> List[List[PermissionChange]]:
        """枚举所有版本非递减的变更顺序(同版本内部任意)。"""
        by_version: Dict[int, List[PermissionChange]] = {}
        for c in self._changes:
            by_version.setdefault(c.version, []).append(c)
        groups = [by_version[v] for v in sorted(by_version)]
        per_group_perms = [list(itertools.permutations(g)) for g in groups]
        orders = []
        for combo in itertools.product(*per_group_perms):
            order: List[PermissionChange] = []
            for part in combo:
                order.extend(part)
            orders.append(order)
        return orders or [[]]

    def _states_for_order(self, order: List[PermissionChange]) -> List[Snapshot]:
        """states[k] = 顺序中前 k 个变更生效后的授权快照。"""
        base: Snapshot = {
            sid: dict(resources)
            for sid, resources in self._input.initial_permissions.items()
        }
        for s in self._input.subjects:
            base.setdefault(s.id, {})
        states: List[Snapshot] = [self._copy(base)]
        current = base
        for change in order:
            nxt = self._copy(current)
            for member in self._input.members_of(change.group):
                allowed = set(nxt[member].get(change.resource, frozenset()))
                allowed |= set(change.grant)
                allowed -= set(change.revoke)
                nxt[member][change.resource] = frozenset(allowed)
            states.append(nxt)
            current = nxt
        return states

    @staticmethod
    def _copy(snapshot: Snapshot) -> Snapshot:
        return {sid: dict(res) for sid, res in snapshot.items()}

    @staticmethod
    def _slot_versions(order: List[PermissionChange]) -> List[int]:
        """slot k(前 k 个变更已生效)对应的状态版本。"""
        versions = [0]
        current = 0
        for c in order:
            current = max(current, c.version)
            versions.append(current)
        return versions

    def _search_times(
        self,
        order: List[PermissionChange],
        states: List[Snapshot],
        slot_versions: List[int],
        reads: Sequence[ReadRecord],
    ) -> Optional[Tuple[List[float], Dict[str, ReadWitness]]]:
        """在固定顺序下,为每个变更在窗口内选非递减的生效时刻。

        候选时刻取所有窗口/调用区间端点:约束均为上下界形式,
        若存在可行解,则存在取端点的可行解。
        """
        n = len(order)
        endpoints = {c.window[0] for c in order} | {c.window[1] for c in order}
        for r in reads:
            endpoints.add(r.start)
            endpoints.add(r.end)
        ordered_points = sorted(endpoints)

        candidates: List[List[float]] = []
        for c in order:
            lo, hi = c.window
            pts = [p for p in ordered_points if lo <= p <= hi]
            if not pts:  # 窗口内无候选端点(空窗口已在校验中排除)
                pts = [lo]
            candidates.append(pts)

        budget = [_MAX_ASSIGNMENTS]
        chosen: List[float] = [0.0] * n

        def dfs(i: int, floor: float) -> Optional[Tuple[List[float], Dict[str, ReadWitness]]]:
            if budget[0] <= 0:
                return None
            if i == n:
                budget[0] -= 1
                wits = self._check_reads(reads, chosen, states, slot_versions)
                if wits is not None:
                    return list(chosen), wits
                return None
            for t in candidates[i]:
                if t < floor:
                    continue
                chosen[i] = t
                found = dfs(i + 1, t)
                if found is not None:
                    return found
            return None

        return dfs(0, float("-inf"))

    def _check_reads(
        self,
        reads: Sequence[ReadRecord],
        times: List[float],
        states: List[Snapshot],
        slot_versions: List[int],
    ) -> Optional[Dict[str, ReadWitness]]:
        witnesses: Dict[str, ReadWitness] = {}
        n = len(times)
        for read in reads:
            wit = None
            for k in range(n + 1):
                lo = times[k - 1] if k > 0 else float("-inf")
                hi = times[k] if k < n else float("inf")
                # 观察时刻 t 需满足 read.start <= t <= read.end 且 lo <= t <= hi。
                t = max(read.start, lo)
                if t > min(read.end, hi):
                    continue
                if read.authz_version is not None and slot_versions[k] != read.authz_version:
                    continue
                allowed = states[k].get(read.subject, {}).get(read.resource, frozenset())
                if read.returned <= allowed:
                    wit = ReadWitness(
                        read_id=read.id,
                        observed_at=t,
                        state_version=slot_versions[k],
                        slot=k,
                    )
                    break
            if wit is None:
                return None
            witnesses[read.id] = wit
        return witnesses

    # ------------------------------------------------------- 结果构造

    def _consistent_result(
        self,
        order: List[PermissionChange],
        times: List[float],
        read_wits: Dict[str, ReadWitness],
    ) -> AuditResult:
        reasons = ["存在符合规则的调度:"]
        for c, t in zip(order, times):
            reasons.append(
                f"变更 {c.id}(版本 {c.version})于 t={t} 生效,窗口 [{c.window[0]}, {c.window[1]}]"
            )
        for rid, w in read_wits.items():
            reasons.append(
                f"读取 {rid} 于 t={w.observed_at} 观察到版本 {w.state_version} 的完整授权状态"
            )

        pending = self._receipts_affecting_conclusion(order)
        if pending:
            reasons.append(
                "但以下变更缺失生效回执,且读取结论依赖其确切生效时刻,转人工复核"
            )
            return AuditResult(
                verdict=Verdict.MANUAL_REVIEW,
                change_times={c.id: t for c, t in zip(order, times)},
                read_witnesses=read_wits,
                pending_receipts=tuple(c.id for c in pending),
                reasons=tuple(reasons),
            )
        return AuditResult(
            verdict=Verdict.CONSISTENT,
            change_times={c.id: t for c, t in zip(order, times)},
            read_witnesses=read_wits,
            reasons=tuple(reasons),
        )

    def _receipts_affecting_conclusion(
        self, order: List[PermissionChange]
    ) -> List[PermissionChange]:
        """缺失回执、且某条读取的可允许字段集在变更前后不同、
        调用时段又与变更窗口重叠的变更 —— 结论依赖其确切生效时刻。"""
        affecting: List[PermissionChange] = []
        for change in order:
            if change.receipt:
                continue
            members = set(self._input.members_of(change.group))
            for read in self._reads:
                if read.subject not in members or read.resource != change.resource:
                    continue
                if read.end < change.window[0] or read.start > change.window[1]:
                    continue
                pre = self._allowed_before(change, read.subject, read.resource)
                post = (set(pre) | set(change.grant)) - set(change.revoke)
                before_ok = read.returned <= pre
                after_ok = read.returned <= post
                if before_ok != after_ok:
                    affecting.append(change)
                    break
        return affecting

    def _allowed_before(
        self, change: PermissionChange, subject: str, resource: str
    ) -> FrozenSet[str]:
        """按版本序,该变更生效前主体已确定拥有的字段(仅考虑更早版本)。"""
        allowed = set(
            self._input.initial_permissions.get(subject, {}).get(resource, frozenset())
        )
        for other in sorted(self._changes, key=lambda c: c.version):
            if other.version >= change.version:
                break
            if other.resource != resource:
                continue
            if subject not in self._input.members_of(other.group):
                continue
            allowed |= set(other.grant)
            allowed -= set(other.revoke)
        return frozenset(allowed)

    def _conflict_result(self) -> AuditResult:
        minimal = self._minimal_conflict_subset()
        involved = self._involved_changes(minimal)
        reasons = ["所有变更顺序与时刻安排均矛盾,最小冲突记录集合:"]
        for r in minimal:
            reasons.append(
                f"读取 {r.id}: 主体 {r.subject} 资源 {r.resource} "
                f"时段 [{r.start}, {r.end}] 返回 {sorted(r.returned)}"
                + (
                    f" 声明版本 {r.authz_version}"
                    if r.authz_version is not None
                    else ""
                )
            )
        unreceipted = [c for c in involved if not c.receipt]
        if unreceipted:
            reasons.append(
                "冲突定位依赖以下缺失回执的变更的确切生效时刻,转人工复核"
            )
            return AuditResult(
                verdict=Verdict.MANUAL_REVIEW,
                conflicting_reads=tuple(r.id for r in minimal),
                involved_changes=tuple(c.id for c in involved),
                pending_receipts=tuple(c.id for c in unreceipted),
                reasons=tuple(reasons),
            )
        return AuditResult(
            verdict=Verdict.CONFLICT,
            conflicting_reads=tuple(r.id for r in minimal),
            involved_changes=tuple(c.id for c in involved),
            reasons=tuple(reasons),
        )

    def _minimal_conflict_subset(self) -> List[ReadRecord]:
        """贪心删除法求最小不可满足读取子集。"""
        remaining = list(self._reads)
        for read in list(remaining):
            trial = [r for r in remaining if r is not read]
            if self._find_witness(trial) is None:
                remaining = trial
        return remaining

    def _involved_changes(self, reads: Sequence[ReadRecord]) -> List[PermissionChange]:
        """与冲突记录相关(同时段、同资源、同组成员)的变更。"""
        involved = []
        for change in self._changes:
            members = set(self._input.members_of(change.group))
            for read in reads:
                if read.subject not in members or read.resource != change.resource:
                    continue
                if read.end < change.window[0] or read.start > change.window[1]:
                    continue
                involved.append(change)
                break
        return involved
