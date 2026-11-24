"""不可退集合的极小性诊断。

可行性关于退役集合是单调的：若退役集合 R 不可行，则任何 R 的超集也不可行
（要腾空的节点更多，约束只会更紧）。因此对不可行集合逐个尝试移除成员，
留下“移除任意一个成员即可行”的极小核，并为核中每个成员保存移除后可执行的
计划作为依据。
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Plan, PlanningError, Topology
from .planner import plan_for_retirement


@dataclass(frozen=True)
class RemovalWitness:
    """从不可退核中移除 node 后，剩余退役集合即可执行的依据。"""

    node: str
    remaining: frozenset[str]
    plan: Plan


@dataclass(frozen=True)
class IrreducibleCore:
    """按包含关系极小的不可退子集及逐成员移除依据。"""

    core: frozenset[str]
    witnesses: tuple[RemovalWitness, ...]

    def is_irreducible(self) -> bool:
        return all(w.remaining == self.core - {w.node} for w in self.witnesses)


def explain_irreducible(
    topology: Topology,
    requested: set[str] | frozenset[str],
    *,
    max_states: int = 200_000,
    max_actions: int = 1_000_000,
) -> IrreducibleCore:
    """返回 requested（若整体不可退）的一个极小不可退子集与移除依据。

    若 requested 本身可行，抛出 :class:`PlanningError`——可退集合无需诊断。
    遍历顺序按节点 id 固定，保证同一输入得到确定性的核。
    """
    requested = frozenset(requested)
    unknown = requested - {n.id for n in topology.nodes}
    if unknown:
        raise PlanningError(f"诊断集合包含未知节点 {sorted(unknown)}")

    first = plan_for_retirement(
        topology, requested,
        max_states=max_states, max_actions=max_actions,
    )
    if first is not None:
        raise PlanningError(f"退役集合 {sorted(requested)} 本身可行，无需诊断")

    # 单调收缩：可行性关于退役集合是下闭的（集合越小越容易可行）。
    # 不变量——core 始终不可行。对每个成员 x：
    #   core\{x} 可行 -> x 是必要成员，保留；
    #   core\{x} 仍不可行 -> x 可删，核在剩余集合中继续找。
    core = set(requested)
    for node in sorted(requested):
        if node not in core:
            continue
        trial = frozenset(core - {node})
        plan = plan_for_retirement(
            topology, trial,
            max_states=max_states, max_actions=max_actions,
        )
        if plan is None:
            core.remove(node)

    witnesses: list[RemovalWitness] = []
    core_frozen = frozenset(core)
    for node in sorted(core_frozen):
        remaining = core_frozen - {node}
        plan = plan_for_retirement(
            topology, remaining,
            max_states=max_states, max_actions=max_actions,
        )
        if plan is None:  # pragma: no cover - 收缩终止条件保证可行
            raise PlanningError("内部错误：极小核的单次移除应当可行")
        witnesses.append(RemovalWitness(node, remaining, plan))

    return IrreducibleCore(core_frozen, tuple(witnesses))
