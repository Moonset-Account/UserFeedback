"""声明式规则求值引擎。

给定一组当前有效观测和一个结论规格 :class:`ConclusionSpec`，
:func:`evaluate_conclusion` 判定该结论是否成立：

1. 每条 ``Requirement`` 先在同事实、证据等级达标的观测中筛选候选；
   没有候选即产生 ``below_threshold`` 缺口（同时记录等级最高但不达标的观测，
   供迁移模块生成复核项）。
2. 所有带 ``independent_source=True`` 的要求（跨结论规格统一处理）必须分配到
   互不相同的 ``source_id``；非独立要求不参与该约束。
3. 独立来源分配用带回溯的深度优先搜索完成；无解时给出
   ``lacks_independent_source`` 缺口，说明冲突在哪些来源之间。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import (
    ConclusionSpec,
    Observation,
    Requirement,
    evidence_rank,
)


@dataclass(frozen=True)
class Deficit:
    """结论不成立时的单项缺口。"""

    spec_id: str
    req_index: int
    fact: str
    reason: str  # below_threshold | lacks_independent_source
    min_level: str
    candidate_obs_ids: tuple[str, ...] = ()
    candidate_sources: tuple[str, ...] = ()
    detail: str = ""


@dataclass
class Evaluation:
    """一次结论求值结果。"""

    spec_id: str
    satisfied: bool
    assignments: dict[int, str] = field(default_factory=dict)
    deficits: list[Deficit] = field(default_factory=list)

    @property
    def used_obs_ids(self) -> tuple[str, ...]:
        return tuple(self.assignments[idx] for idx in sorted(self.assignments))


def _eligible_obs(
    observations: list[Observation], req: Requirement
) -> list[Observation]:
    """同事实且证据等级达标的候选观测，按等级降序、id 升序保证确定性。"""
    threshold = evidence_rank(req.min_level)
    candidates = [
        obs
        for obs in observations
        if obs.fact == req.fact and evidence_rank(obs.evidence_level) >= threshold
    ]
    candidates.sort(key=lambda o: (-evidence_rank(o.evidence_level), o.obs_id))
    return candidates


def _best_below_threshold(
    observations: list[Observation], req: Requirement
) -> list[Observation]:
    """事实匹配但等级不足的观测里等级最高的那些（用于复核提示）。"""
    threshold = evidence_rank(req.min_level)
    weaker = [
        obs
        for obs in observations
        if obs.fact == req.fact and evidence_rank(obs.evidence_level) < threshold
    ]
    if not weaker:
        return []
    best = max(evidence_rank(o.evidence_level) for o in weaker)
    return sorted(
        (o for o in weaker if evidence_rank(o.evidence_level) == best),
        key=lambda o: o.obs_id,
    )


@dataclass
class _Slot:
    spec_id: str
    req_index: int
    req: Requirement
    candidates: list[Observation]
    global_scope: bool


def _build_slots(
    specs: list[ConclusionSpec],
    observations: list[Observation],
) -> tuple[list[_Slot], list[Deficit]]:
    slots: list[_Slot] = []
    deficits: list[Deficit] = []
    for spec in specs:
        for index, req in enumerate(spec.requires):
            candidates = _eligible_obs(observations, req)
            if not candidates:
                weak = _best_below_threshold(observations, req)
                deficits.append(
                    Deficit(
                        spec_id=spec.conclusion_id,
                        req_index=index,
                        fact=req.fact,
                        reason="below_threshold",
                        min_level=req.min_level,
                        candidate_obs_ids=tuple(o.obs_id for o in weak),
                        candidate_sources=tuple(o.source_id for o in weak),
                        detail=(
                            f"事实 {req.fact} 缺少等级不低于 {req.min_level} 的观测"
                        ),
                    )
                )
                continue
            slots.append(
                _Slot(
                    spec_id=spec.conclusion_id,
                    req_index=index,
                    req=req,
                    candidates=candidates,
                    global_scope=req.independent_source,
                )
            )
    return slots, deficits


def _solve_independent_sources(
    slots: list[_Slot],
) -> tuple[dict[tuple[str, int], str], list[Deficit]]:
    """为独立来源槽位分配不同 source_id；非独立槽位取各自最优候选。

    返回 ``{(spec_id, req_index): obs_id}`` 与无解时的缺口列表。
    """
    assignments: dict[tuple[str, int], str] = {}
    deficits: list[Deficit] = []

    local_slots = [s for s in slots if not s.global_scope]
    for slot in local_slots:
        assignments[(slot.spec_id, slot.req_index)] = slot.candidates[0].obs_id

    global_slots = [s for s in slots if s.global_scope]
    # 候选少（约束最紧）的槽位优先，提升剪枝效率与结果稳定性。
    global_slots.sort(key=lambda s: (len({o.source_id for o in s.candidates}),))

    ordered_sources: list[list[Observation]] = []
    for slot in global_slots:
        seen: dict[str, Observation] = {}
        for obs in slot.candidates:  # 已按等级降序排列
            seen.setdefault(obs.source_id, obs)
        ordered_sources.append(list(seen.values()))

    chosen: list[Observation] = []

    def backtrack(pos: int, used_sources: set[str]) -> bool:
        if pos == len(global_slots):
            return True
        slot = global_slots[pos]
        for obs in ordered_sources[pos]:
            if obs.source_id in used_sources:
                continue
            chosen.append(obs)
            used_sources.add(obs.source_id)
            if backtrack(pos + 1, used_sources):
                return True
            used_sources.remove(obs.source_id)
            chosen.pop()
        return False

    if backtrack(0, set()):
        for slot, obs in zip(global_slots, chosen):
            assignments[(slot.spec_id, slot.req_index)] = obs.obs_id
        return assignments, deficits

    for slot in global_slots:
        available = sorted({o.source_id for o in slot.candidates})
        deficits.append(
            Deficit(
                spec_id=slot.spec_id,
                req_index=slot.req_index,
                fact=slot.req.fact,
                reason="lacks_independent_source",
                min_level=slot.req.min_level,
                candidate_obs_ids=tuple(o.obs_id for o in slot.candidates),
                candidate_sources=tuple(available),
                detail=(
                    f"独立来源要求无法满足：事实 {slot.req.fact} 仅能取得来源 "
                    f"{', '.join(available)}，需要与其它独立要求使用不同来源"
                ),
            )
        )
    return assignments, deficits


def evaluate_conclusion(
    spec: ConclusionSpec, observations: list[Observation]
) -> Evaluation:
    """评估单个结论规格。"""
    return evaluate_all([spec], observations)[spec.conclusion_id]


def evaluate_all(
    specs: list[ConclusionSpec], observations: list[Observation]
) -> dict[str, Evaluation]:
    """同时评估多个结论规格。

    独立来源约束在所有带标记的要求之间全局生效（用于 split 场景下
    各子结论必须来自不同来源的核验）。
    """
    slots, deficits = _build_slots(specs, observations)
    assignments, solve_deficits = _solve_independent_sources(slots)
    deficits.extend(solve_deficits)

    result: dict[str, Evaluation] = {}
    for spec in specs:
        spec_assignments = {
            req_index: obs_id
            for (spec_id, req_index), obs_id in assignments.items()
            if spec_id == spec.conclusion_id
        }
        spec_deficits = [d for d in deficits if d.spec_id == spec.conclusion_id]
        result[spec.conclusion_id] = Evaluation(
            spec_id=spec.conclusion_id,
            satisfied=not spec_deficits,
            assignments=spec_assignments,
            deficits=spec_deficits,
        )
    return result
