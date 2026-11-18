"""案情数据组件：设备线索归并（CaseBoard）。

调查员收集到设备登记、临时别名和人工确认记录，本组件负责把指向
同一设备的线索合并，并保证以下约束：

1. 确认关联（强）：已核实，参与分组归并，可积累。
2. 互斥证据（强负向）：明确不可同设备，永久保存，不因归并而丢失。
3. 弱别名：仅提示候选，绝不参与分组。
4. 强关联路径若与互斥证据冲突：分组标记为“待人工”，并给出冲突链
   （连接互斥两端的确认关联路径）。直接与互斥证据矛盾的确认关联
   会被拒绝（ConflictError），不会写入。
5. 撤回一条已确认关联：只重建受影响的分组，旧分组状态存入快照，
   每条线索的来源（source）随证据与快照一并保留，供复核原归因。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from enum import Enum


class EvidenceKind(str, Enum):
    CONFIRMED_LINK = "confirmed_link"  # 已核实关联（强）
    EXCLUSION = "exclusion"            # 互斥证据（强负向）
    WEAK_ALIAS = "weak_alias"          # 弱别名（仅候选）


class GroupStatus(str, Enum):
    OK = "ok"
    PENDING_REVIEW = "pending_review"  # 待人工复核


class ConflictError(ValueError):
    """确认关联与既有互斥证据直接冲突，已被拒绝。"""

    def __init__(self, message: str, exclusion_id: str) -> None:
        super().__init__(message)
        self.exclusion_id = exclusion_id


@dataclass(frozen=True)
class Evidence:
    """一条线索。source 记录来源（如 设备登记/人工确认/临时别名）。"""

    id: str
    kind: EvidenceKind
    a: str
    b: str
    source: str
    note: str = ""
    active: bool = True
    retracted_reason: str | None = None


@dataclass(frozen=True)
class Conflict:
    """强关联路径与互斥证据的冲突。chain 为实体路径，chain_links 为
    构成该路径的确认关联证据 id，供人工复核冲突链。"""

    exclusion_id: str
    endpoints: tuple[str, str]
    chain: tuple[str, ...]
    chain_links: tuple[str, ...]


@dataclass(frozen=True)
class Group:
    """当前分组（同一设备的线索集合）。"""

    id: str
    version: int
    members: frozenset[str]
    status: GroupStatus
    conflicts: tuple[Conflict, ...] = ()


@dataclass(frozen=True)
class GroupSnapshot:
    """分组被重建前的旧快照。provenance 保留当时组内全部线索
    （含来源与撤回信息），供复核原归因。"""

    group_id: str
    version: int
    members: tuple[str, ...]
    status: GroupStatus
    reason: str
    provenance: tuple[Evidence, ...]


@dataclass(frozen=True)
class Candidate:
    """弱别名候选：只提示，不参与分组。"""

    evidence_id: str
    a: str
    b: str
    source: str
    note: str
    same_group: bool | None  # 两端当前是否已在同一分组；任一端未分组则为 None


class CaseBoard:
    """案情数据组件入口。"""

    def __init__(self) -> None:
        self._evidence: dict[str, Evidence] = {}
        self._groups: dict[str, Group] = {}
        self._snapshots: list[GroupSnapshot] = []
        self._seq = 0
        self._group_seq = 0

    # ------------------------------------------------------------------ 录入

    def confirm_link(self, a: str, b: str, *, source: str, note: str = "") -> str:
        """积累一条已核实关联。若与互斥证据直接冲突则拒绝。"""
        self._check_pair(a, b)
        hit = self._find_active_exclusion(a, b)
        if hit is not None:
            raise ConflictError(
                f"确认关联 {a!r}<->{b!r} 与互斥证据 {hit.id} 直接冲突，已拒绝",
                exclusion_id=hit.id,
            )
        existing = self._find_active_link(a, b)
        if existing is not None:
            return existing.id  # 幂等：重复确认不重复记录
        ev = self._record(EvidenceKind.CONFIRMED_LINK, a, b, source, note)
        self._rebuild(self._affected_entities({a, b}), reason=f"confirm:{ev.id}")
        return ev.id

    def add_exclusion(self, a: str, b: str, *, source: str, note: str = "") -> str:
        """保存一条明确的互斥证据。若两端已被强关联路径连通，
        所在分组会被标记为待人工并给出冲突链。"""
        self._check_pair(a, b)
        ev = self._record(EvidenceKind.EXCLUSION, a, b, source, note)
        self._rebuild(self._affected_entities({a, b}), reason=f"exclusion:{ev.id}")
        return ev.id

    def suggest_alias(self, a: str, b: str, *, source: str, note: str = "") -> str:
        """登记弱别名：仅提示候选，不触发任何分组变化。"""
        self._check_pair(a, b)
        return self._record(EvidenceKind.WEAK_ALIAS, a, b, source, note).id

    # ------------------------------------------------------------------ 撤回

    def retract_link(self, evidence_id: str, *, reason: str) -> None:
        """撤回一条已确认关联。只重建受影响分组，旧状态存入快照。"""
        if evidence_id not in self._evidence:
            raise KeyError(f"未知证据 id: {evidence_id}")
        ev = self._evidence[evidence_id]
        if ev.kind is not EvidenceKind.CONFIRMED_LINK:
            raise ValueError(f"只能撤回已确认关联，{evidence_id} 是 {ev.kind.value}")
        if not ev.active:
            raise ValueError(f"证据 {evidence_id} 已被撤回")
        self._evidence[evidence_id] = replace(
            ev, active=False, retracted_reason=reason
        )
        self._rebuild(
            self._affected_entities({ev.a, ev.b}), reason=f"retract:{evidence_id}"
        )

    # ------------------------------------------------------------------ 查询

    def groups(self) -> tuple[Group, ...]:
        return tuple(self._groups[gid] for gid in sorted(self._groups))

    def group_of(self, entity: str) -> Group | None:
        for g in self._groups.values():
            if entity in g.members:
                return g
        return None

    def candidates(self) -> tuple[Candidate, ...]:
        """弱别名候选列表（只提示，不参与分组）。"""
        out = []
        for ev in self._evidence.values():
            if ev.kind is not EvidenceKind.WEAK_ALIAS or not ev.active:
                continue
            ga, gb = self.group_of(ev.a), self.group_of(ev.b)
            same = None if ga is None or gb is None else ga.id == gb.id
            out.append(Candidate(ev.id, ev.a, ev.b, ev.source, ev.note, same))
        return tuple(out)

    def pending_reviews(self) -> tuple[tuple[str, Conflict], ...]:
        """所有待人工复核的 (分组 id, 冲突) 列表。"""
        return tuple(
            (g.id, c)
            for g in self.groups()
            if g.status is GroupStatus.PENDING_REVIEW
            for c in g.conflicts
        )

    def snapshots(self, group_id: str | None = None) -> tuple[GroupSnapshot, ...]:
        if group_id is None:
            return tuple(self._snapshots)
        return tuple(s for s in self._snapshots if s.group_id == group_id)

    def evidence(self, evidence_id: str) -> Evidence:
        return self._evidence[evidence_id]

    def provenance(self, entity: str) -> tuple[Evidence, ...]:
        """某设备线索的全部来源记录（含已撤回的，按登记顺序）。"""
        return tuple(
            ev for ev in self._evidence.values() if entity in (ev.a, ev.b)
        )

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _check_pair(a: str, b: str) -> None:
        if not a or not b or a == b:
            raise ValueError(f"非法线索对: {a!r}, {b!r}")

    def _record(
        self, kind: EvidenceKind, a: str, b: str, source: str, note: str
    ) -> Evidence:
        self._seq += 1
        ev = Evidence(id=f"E{self._seq}", kind=kind, a=a, b=b, source=source, note=note)
        self._evidence[ev.id] = ev
        return ev

    def _find_active_exclusion(self, a: str, b: str) -> Evidence | None:
        for ev in self._evidence.values():
            if ev.kind is EvidenceKind.EXCLUSION and ev.active:
                if {ev.a, ev.b} == {a, b}:
                    return ev
        return None

    def _find_active_link(self, a: str, b: str) -> Evidence | None:
        for ev in self._evidence.values():
            if ev.kind is EvidenceKind.CONFIRMED_LINK and ev.active:
                if {ev.a, ev.b} == {a, b}:
                    return ev
        return None

    def _affected_entities(self, seeds: set[str]) -> set[str]:
        """受影响实体 = 种子实体及其所在分组的全部成员（分组互不相交）。"""
        out = set(seeds)
        for g in self._groups.values():
            if g.members & out:
                out |= g.members
        return out

    def _active_links_within(self, entities: set[str]) -> list[Evidence]:
        return [
            ev
            for ev in self._evidence.values()
            if ev.kind is EvidenceKind.CONFIRMED_LINK
            and ev.active
            and ev.a in entities
            and ev.b in entities
        ]

    def _components_within(self, entities: set[str]) -> list[set[str]]:
        parent = {e: e for e in entities}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for ev in self._active_links_within(entities):
            ra, rb = find(ev.a), find(ev.b)
            if ra != rb:
                parent[ra] = rb
        comps: dict[str, set[str]] = {}
        for e in entities:
            comps.setdefault(find(e), set()).add(e)
        return list(comps.values())

    def _conflicts_in(self, comp: set[str]) -> list[Conflict]:
        adj: dict[str, list[tuple[str, str]]] = {}
        for ev in self._active_links_within(comp):
            adj.setdefault(ev.a, []).append((ev.b, ev.id))
            adj.setdefault(ev.b, []).append((ev.a, ev.id))
        out = []
        for ev in self._evidence.values():
            if ev.kind is not EvidenceKind.EXCLUSION or not ev.active:
                continue
            if ev.a in comp and ev.b in comp:
                chain, links = self._link_path(ev.a, ev.b, adj)
                out.append(
                    Conflict(
                        exclusion_id=ev.id,
                        endpoints=(ev.a, ev.b),
                        chain=chain,
                        chain_links=links,
                    )
                )
        return out

    @staticmethod
    def _link_path(
        start: str, goal: str, adj: dict[str, list[tuple[str, str]]]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """BFS 最短路径：返回 (实体路径, 沿途确认关联证据 id)。"""
        queue: deque[tuple[str, list[str], list[str]]] = deque([(start, [start], [])])
        seen = {start}
        while queue:
            node, path, links = queue.popleft()
            if node == goal:
                return tuple(path), tuple(links)
            for nxt, eid in adj.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append((nxt, path + [nxt], links + [eid]))
        return (), ()

    def _snapshot_of(self, group: Group, reason: str) -> GroupSnapshot:
        related = tuple(
            ev
            for ev in self._evidence.values()
            if ev.a in group.members and ev.b in group.members
        )
        return GroupSnapshot(
            group_id=group.id,
            version=group.version,
            members=tuple(sorted(group.members)),
            status=group.status,
            reason=reason,
            provenance=related,
        )

    def _rebuild(self, affected: set[str], reason: str) -> None:
        """只重建受影响分组：归档旧快照，按当前有效确认关联重新切分，
        未受影响的分组保持原 id 与版本不变。"""
        touched = [g for g in self._groups.values() if g.members & affected]
        base_version = 0
        for g in touched:
            self._snapshots.append(self._snapshot_of(g, reason))
            base_version = max(base_version, g.version)
            del self._groups[g.id]
        for comp in self._components_within(affected):
            if len(comp) < 2:
                continue  # 孤立线索不构成分组
            self._group_seq += 1
            conflicts = tuple(self._conflicts_in(comp))
            status = GroupStatus.PENDING_REVIEW if conflicts else GroupStatus.OK
            self._groups[f"G{self._group_seq}"] = Group(
                id=f"G{self._group_seq}",
                version=base_version + 1,
                members=frozenset(comp),
                status=status,
                conflicts=conflicts,
            )
