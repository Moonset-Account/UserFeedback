"""授权事实数据流分析引擎。

分析在 :mod:`authflow.model` 定义的 CFG 上执行单调前向数据流计算，
回答题面的核心问题：**每个持久化写入位置能否证明受同一对象、同一版本、
同一操作上的成功授权事实约束？**

语义约定
========
1. 授权事实是 ``(对象, 版本, 操作)`` 三元组（:class:`FactKey`），由
   :class:`~authflow.model.AuthCheckNode` 产生，并记录产生它的检查节点 id。
2. 事实沿可执行路径传播；多前驱合流取**交集**——事实必须在所有到达路径
   上成立。合流丢失的绑定/版本/事实记录为 :class:`MergeGap`。
3. 调用摘要声明的对象替换 / 版本推进使旧版本上的事实立即失效，失效记录
   保存在 :class:`InvalidatedFact` 中（保留失效原因与位置）。
4. 别名共享引用与事实；对象副本获得新对象标识，事实不复制。
5. 事务回滚恢复事务开始时的绑定/版本/事实快照，事务内的事实按
   "回滚失效"语义进入失效记录；提交则保留。
6. 不可达节点不参与数据流；不可达调用单独报告，与可达但无授权的写入
   （:class:`Finding`）严格区分。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from authflow.errors import ProgramStructureError
from authflow.model import (
    AliasNode,
    AuthCheckNode,
    BranchNode,
    CallNode,
    EntryNode,
    MergeNode,
    Node,
    Program,
    TxBeginNode,
    TxCommitNode,
    TxRollbackNode,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "FactKey",
    "InvalidatedFact",
    "VersionEvent",
    "BindingEvent",
    "MergeGap",
    "WriteEvaluation",
    "ChainLink",
    "Finding",
    "AnalysisResult",
    "Env",
    "analyze",
]


# --------------------------------------------------------------------------
# 事实与证据记录
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FactKey:
    """授权事实三元组：对象标识、版本、操作。"""

    object_id: str
    version: int
    operation: str


@dataclass(frozen=True)
class InvalidatedFact:
    """一条授权事实失效的证据记录。

    Attributes:
        fact: 失效的事实三元组。
        origins: 产生该事实的授权检查节点 id 集合。
        reason: 失效原因，取值见模块常量（``REASON_*``）。
        node_id: 导致失效的节点 id（调用节点 / 回滚节点）。
        detail: 人类可读说明。
    """

    fact: FactKey
    origins: frozenset[str]
    reason: str
    node_id: str
    detail: str


@dataclass(frozen=True)
class VersionEvent:
    """对象版本谱系上的一个事件，用于断链定位。"""

    node_id: str
    object_id: str
    old_version: int | None
    new_version: int | None
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class BindingEvent:
    """变量绑定变化事件（重绑定 / 解绑 / 副本 / 别名）。"""

    node_id: str
    variable: str
    old_binding: tuple[str, int] | None
    new_binding: tuple[str, int] | None
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class MergeGap:
    """合流点上路径间不一致的证据（断开依赖链的交汇位置）。"""

    merge_node_id: str
    variable_diffs: dict[str, tuple[tuple[str, int] | None, ...]]
    object_version_diffs: dict[str, tuple[int, ...]]
    fact_diffs: tuple[FactKey, ...]
    unknown_objects: tuple[str, ...]
    unknown_variables: tuple[str, ...]


@dataclass(frozen=True)
class WriteEvaluation:
    """单个写入调用节点的授权判定结果。"""

    node_id: str
    target: str | None
    object_id: str | None
    version: int | None
    operation: str | None
    authorized: bool
    reason: str
    detail: str
    matched_fact: FactKey | None
    fact_origins: frozenset[str]
    evidence_node_id: str | None


@dataclass(frozen=True)
class ChainLink:
    """断开的依赖链上的一个环节。"""

    node_id: str
    node_kind: str
    role: str
    is_break: bool = False
    note: str = ""


@dataclass(frozen=True)
class Finding:
    """一个无法证明受授权约束的写入位置及其断开的依赖链。"""

    node_id: str
    target: str | None
    object_id: str | None
    version: int | None
    operation: str | None
    reason: str
    detail: str
    chain: tuple[ChainLink, ...]
    matched_fact: FactKey | None = None
    fact_origins: frozenset[str] = frozenset()


# 失效 / 判定原因常量
REASON_OBJECT_REPLACED = "object_replaced"
REASON_VERSION_ADVANCED = "version_advanced"
REASON_TX_ROLLBACK = "tx_rollback"
REASON_TX_COMMIT = "tx_commit"

REASON_UNBOUND = "unbound"
REASON_DIVERGENT_VARIABLE = "divergent_variable"
REASON_DIVERGENT_OBJECT = "divergent_object"
REASON_OBJECT_COPY = "object_copy"
REASON_OVERWRITTEN_BINDING = "overwritten_binding"
REASON_STALE_VERSION = "stale_version"
REASON_OPERATION_MISMATCH = "operation_mismatch"
REASON_NO_AUTHORIZATION = "no_authorization"
REASON_AUTHORIZED = "authorized"


# --------------------------------------------------------------------------
# 事务快照与数据流环境
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class TxSnapshot:
    """事务开始时冻结的环境状态，回滚时据此恢复。"""

    bindings: dict[str, tuple[str, int]]
    binding_origins: dict[str, str]
    facts: dict[FactKey, frozenset[str]]
    unknown_objects: frozenset[str]
    unknown_variables: frozenset[str]


class Env:
    """沿一条路径累积的数据流环境。"""

    def __init__(self) -> None:
        # 变量 -> (对象标识, 版本)
        self.bindings: dict[str, tuple[str, int]] = {}
        # 变量 -> 最近一次建立该绑定的节点 id（用于区分外部覆盖来源）
        self.binding_origins: dict[str, str] = {}
        # 授权事实 -> 产生它的授权检查节点 id 集合
        self.facts: dict[FactKey, frozenset[str]] = {}
        # 失效证据 / 谱系事件 / 绑定事件 / 合流差异（累积日志）
        self.invalidated: list[InvalidatedFact] = []
        self.history: list[VersionEvent] = []
        self.binding_events: list[BindingEvent] = []
        self.gaps: list[MergeGap] = []
        # 事务嵌套栈
        self.tx_stack: list[TxSnapshot] = []
        # 合流后来源不一致、无法安全取交集的对象/变量
        self.unknown_objects: set[str] = set()
        self.unknown_variables: set[str] = set()

    def clone(self) -> "Env":
        """深拷贝（节点转移时在副本上修改，保证前驱环境不被污染）。"""
        env = Env()
        env.bindings = dict(self.bindings)
        env.binding_origins = dict(self.binding_origins)
        env.facts = {k: frozenset(v) for k, v in self.facts.items()}
        env.invalidated = list(self.invalidated)
        env.history = list(self.history)
        env.binding_events = list(self.binding_events)
        env.gaps = list(self.gaps)
        env.tx_stack = list(self.tx_stack)
        env.unknown_objects = set(self.unknown_objects)
        env.unknown_variables = set(self.unknown_variables)
        return env

    def snapshot_tx(self) -> TxSnapshot:
        return TxSnapshot(
            bindings=dict(self.bindings),
            binding_origins=dict(self.binding_origins),
            facts={k: frozenset(v) for k, v in self.facts.items()},
            unknown_objects=frozenset(self.unknown_objects),
            unknown_variables=frozenset(self.unknown_variables),
        )

    def fact_objects(self) -> frozenset[str]:
        return frozenset(k.object_id for k in self.facts)


# --------------------------------------------------------------------------
# 分析结果
# --------------------------------------------------------------------------
@dataclass
class AnalysisResult:
    """:func:`analyze` 的完整输出。"""

    program: Program
    findings: list[Finding]
    constrained_writes: list[str]
    reachable_calls: list[str]
    unreachable_calls: list[str]
    evaluations: dict[str, WriteEvaluation]
    invalidated_facts: list[InvalidatedFact]
    merge_gaps: list[MergeGap]
    input_envs: dict[str, Env]
    output_envs: dict[str, Env]

    def facts_at_exit(self) -> dict[FactKey, frozenset[str]]:
        """返回所有叶子节点输出环境中仍然存活的授权事实。"""
        alive: dict[FactKey, frozenset[str]] = {}
        for nid, node in self.program.nodes.items():
            if self.program.successors_of(nid):
                continue
            env = self.output_envs.get(nid)
            if env is None:
                continue
            for key, origins in env.facts.items():
                alive[key] = alive.get(key, frozenset()) | origins
        return alive


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def analyze(program: Program) -> AnalysisResult:
    """对给定程序执行授权绑定静态分析。

    Args:
        program: 由 :class:`~authflow.model.ProgramBuilder` 构建的 DAG 程序。

    Returns:
        :class:`AnalysisResult`，包含无授权约束写入、断开的依赖链、
        失效事实、可达/不可达调用区分等全部结果。
    """
    order = program.topological_order()
    reachable = program.reachable_from_entry()

    input_envs: dict[str, Env] = {}
    output_envs: dict[str, Env] = {}
    evaluations: dict[str, WriteEvaluation] = {}

    for node_id in order:
        node = program.nodes[node_id]
        preds = program.predecessors_of(node_id)
        if node_id not in reachable:
            # 不可达节点不参与数据流，也不产生判定
            continue
        if not preds:
            if node_id != program.entry_id:
                raise ProgramStructureError(
                    f"非入口节点 {node_id!r} 没有前驱却从入口可达"
                )
            in_env = Env()
        else:
            pred_envs = [
                output_envs[pid]
                for pid, _label in preds
                if pid in output_envs
            ]
            if not pred_envs:
                continue
            in_env = (
                pred_envs[0].clone()
                if len(pred_envs) == 1
                else _merge_envs(pred_envs, node_id, node)
            )
        input_envs[node_id] = in_env
        out_env = _transfer(node, in_env, program, evaluations)
        output_envs[node_id] = out_env

    # ---- 汇总 -----------------------------------------------------------
    reachable_calls = sorted(
        nid
        for nid in reachable
        if isinstance(program.nodes[nid], CallNode)
    )
    unreachable_calls = sorted(
        nid
        for nid, node in program.nodes.items()
        if isinstance(node, CallNode) and nid not in reachable
    )
    constrained = sorted(
        nid
        for nid, ev in evaluations.items()
        if ev.authorized
    )
    raw_findings = [ev for ev in evaluations.values() if not ev.authorized]
    raw_findings.sort(key=lambda ev: ev.node_id)
    findings = [
        _build_finding(program, ev) for ev in raw_findings
    ]

    invalidated = _dedupe_invalidated(
        item
        for env in output_envs.values()
        for item in env.invalidated
    )
    gaps = _dedupe_gaps(
        gap
        for env in output_envs.values()
        for gap in env.gaps
    )

    return AnalysisResult(
        program=program,
        findings=findings,
        constrained_writes=constrained,
        reachable_calls=reachable_calls,
        unreachable_calls=unreachable_calls,
        evaluations=evaluations,
        invalidated_facts=invalidated,
        merge_gaps=gaps,
        input_envs=input_envs,
        output_envs=output_envs,
    )


# --------------------------------------------------------------------------
# 合流（must 事实取交集）
# --------------------------------------------------------------------------
def _merge_envs(envs: list[Env], node_id: str, node: Node) -> Env:
    base = envs[0]
    merged = Env()
    merged.tx_stack = list(base.tx_stack)

    # 日志类字段直接串联（去重在最终汇总阶段做）
    for env in envs:
        merged.invalidated.extend(env.invalidated)
        merged.history.extend(env.history)
        merged.binding_events.extend(env.binding_events)
        merged.gaps.extend(env.gaps)

    # 绑定：变量与 (oid, version) 完全一致才保留
    first_bindings = base.bindings
    for var, binding in first_bindings.items():
        if all(
            env.bindings.get(var) == binding for env in envs[1:]
        ) and var not in _union_unknown_variables(envs):
            merged.bindings[var] = binding
            origins = env_origins_intersection(envs, var)
            if origins:
                merged.binding_origins[var] = next(iter(origins))
    variable_diffs: dict[str, tuple[tuple[str, int] | None, ...]] = {}
    all_vars = set()
    for env in envs:
        all_vars.update(env.bindings)
        all_vars.update(env.unknown_variables)
    for var in sorted(all_vars):
        views = tuple(
            None if var in env.unknown_variables else env.bindings.get(var)
            for env in envs
        )
        if len(set(views)) > 1:
            variable_diffs[var] = views

    # 事实：三元组交集；来源集合取各路径来源的并集——must 语义只要求
    # 事实在每条路径上都存在，而每条路径上的检查来源可以不同。
    for key in base.facts:
        if all(key in env.facts for env in envs[1:]):
            union: frozenset[str] = frozenset()
            for env in envs:
                union = union | env.facts[key]
            merged.facts[key] = union
    fact_diffs = tuple(
        sorted(
            (
                key
                for env in envs
                for key in env.facts
                if not all(key in other.facts for other in envs)
            ),
            key=lambda k: (k.object_id, k.version, k.operation),
        )
    )

    # 版本视角的对象分歧（同一 oid 在不同路径上版本不同）
    oid_versions: dict[str, set[int]] = {}
    for env in envs:
        for oid, ver in env.bindings.values():
            oid_versions.setdefault(oid, set()).add(ver)
    object_version_diffs = {
        oid: tuple(sorted(versions))
        for oid, versions in sorted(oid_versions.items())
        if len(versions) > 1
    }

    merged.unknown_objects = set().union(
        *(env.unknown_objects for env in envs)
    )
    merged.unknown_variables = set().union(
        *(env.unknown_variables for env in envs)
    )
    # 合流中被丢掉的变量/对象也进入 unknown 集合
    for var in variable_diffs:
        merged.unknown_variables.add(var)
        for env in envs:
            binding = env.bindings.get(var)
            if binding is not None:
                merged.unknown_objects.add(binding[0])
    for oid in object_version_diffs:
        merged.unknown_objects.add(oid)

    if (
        variable_diffs
        or object_version_diffs
        or fact_diffs
    ):
        merged.gaps.append(
            MergeGap(
                merge_node_id=node_id,
                variable_diffs=variable_diffs,
                object_version_diffs=object_version_diffs,
                fact_diffs=fact_diffs,
                unknown_objects=tuple(sorted(merged.unknown_objects)),
                unknown_variables=tuple(sorted(merged.unknown_variables)),
            )
        )
    return merged


def _union_unknown_variables(envs: list[Env]) -> set[str]:
    out: set[str] = set()
    for env in envs:
        out |= env.unknown_variables
    return out


def env_origins_intersection(envs: list[Env], var: str) -> frozenset[str]:
    common: frozenset[str] | None = None
    for env in envs:
        origin = env.binding_origins.get(var)
        if origin is None:
            return frozenset()
        common = frozenset({origin}) if common is None else common & {origin}
    return common or frozenset()


# --------------------------------------------------------------------------
# 节点转移
# --------------------------------------------------------------------------
def _transfer(
    node: Node,
    env: Env,
    program: Program,
    evaluations: dict[str, WriteEvaluation],
) -> Env:
    out = env.clone()
    if isinstance(node, EntryNode):
        for var, binding in node.initial_bindings.items():
            out.bindings[var] = binding
            out.binding_origins[var] = node.node_id
            out.binding_events.append(
                BindingEvent(
                    node_id=node.node_id,
                    variable=var,
                    old_binding=None,
                    new_binding=binding,
                    reason="initial_binding",
                )
            )
    elif isinstance(node, AuthCheckNode):
        _transfer_auth_check(node, out)
    elif isinstance(node, AliasNode):
        _transfer_alias(node, out)
    elif isinstance(node, CallNode):
        evaluation = _evaluate_write(node, out, program)
        if evaluation is not None:
            evaluations[node.node_id] = evaluation
        _apply_call_summary(node, out)
    elif isinstance(node, TxBeginNode):
        out.tx_stack.append(out.snapshot_tx())
    elif isinstance(node, TxCommitNode):
        if not out.tx_stack:
            raise ProgramStructureError(
                f"提交节点 {node.node_id!r} 没有匹配的事务开始节点"
            )
        out.tx_stack.pop()
        out.history.append(
            VersionEvent(
                node_id=node.node_id,
                object_id="",
                old_version=None,
                new_version=None,
                reason=REASON_TX_COMMIT,
                detail="事务提交，事务内授权事实保留",
            )
        )
    elif isinstance(node, TxRollbackNode):
        _apply_rollback(node, out)
    # BranchNode / MergeNode 为纯流节点，无状态变化
    return out


def _transfer_auth_check(node: AuthCheckNode, env: Env) -> None:
    if node.version >= 0:
        version = node.version
        if node.target is not None:
            binding = env.bindings.get(node.target)
            if binding is None:
                raise ProgramStructureError(
                    f"授权检查 {node.node_id!r} 的目标变量 {node.target!r} "
                    "无绑定，无法确认版本"
                )
            if binding[0] != node.object_id:
                raise ProgramStructureError(
                    f"授权检查 {node.node_id!r} 声明对象 {node.object_id!r}，"
                    f"但变量 {node.target!r} 实际绑定 {binding[0]!r}"
                )
            if binding[1] != node.version:
                raise ProgramStructureError(
                    f"授权检查 {node.node_id!r} 显式声明版本 {node.version}，"
                    f"但变量 {node.target!r} 当前版本为 {binding[1]}"
                )
    else:
        if node.target is None:
            raise ProgramStructureError(
                f"授权检查 {node.node_id!r} 使用延迟版本却未提供 target"
            )
        binding = env.bindings.get(node.target)
        if binding is None:
            raise ProgramStructureError(
                f"授权检查 {node.node_id!r} 的目标变量 {node.target!r} "
                "无绑定，无法确认版本"
            )
        if node.object_id != binding[0]:
            raise ProgramStructureError(
                f"授权检查 {node.node_id!r} 声明对象 {node.object_id!r}，"
                f"但变量 {node.target!r} 实际绑定 {binding[0]!r}"
            )
        version = binding[1]
    key = FactKey(node.object_id, version, node.operation)
    env.facts[key] = env.facts.get(key, frozenset()) | {node.node_id}


def _transfer_alias(node: AliasNode, env: Env) -> None:
    rhs_binding = env.bindings.get(node.rhs)
    old = env.bindings.get(node.lhs)
    if rhs_binding is None:
        env.bindings.pop(node.lhs, None)
        env.binding_origins.pop(node.lhs, None)
        # 仅当 lhs 此前确有绑定时，丢失绑定才属于路径发散；从未绑定的
        # 变量保持普通"未绑定"语义。
        if old is not None or node.lhs in env.unknown_variables:
            env.unknown_variables.add(node.lhs)
        env.binding_events.append(
            BindingEvent(
                node_id=node.node_id,
                variable=node.lhs,
                old_binding=old,
                new_binding=None,
                reason=REASON_UNBOUND,
                detail=f"右端 {node.rhs!r} 无绑定，{node.lhs!r} 变为未绑定",
            )
        )
        return
    env.unknown_variables.discard(node.lhs)
    rhs_oid, rhs_ver = rhs_binding
    if node.is_copy:
        assert node.new_object_id is not None
        new_oid = node.new_object_id
        new_ver = (
            node.new_version if node.new_version is not None else rhs_ver
        )
        reason = "object_copy"
        detail = (
            f"{node.lhs!r} 是 {node.rhs!r} 的对象副本，新对象 "
            f"{new_oid!r} 不继承授权事实"
        )
    else:
        new_oid = rhs_oid
        new_ver = (
            node.new_version if node.new_version is not None else rhs_ver
        )
        reason = "alias"
        detail = f"{node.lhs!r} 成为 {node.rhs!r} 的别名，共享对象 {new_oid!r}"
    new_binding = (new_oid, new_ver)
    env.bindings[node.lhs] = new_binding
    env.binding_origins[node.lhs] = node.node_id
    env.binding_events.append(
        BindingEvent(
            node_id=node.node_id,
            variable=node.lhs,
            old_binding=old,
            new_binding=new_binding,
            reason=reason,
            detail=detail,
        )
    )


# --------------------------------------------------------------------------
# 写入授权判定（在应用调用摘要*之前*判定）
# --------------------------------------------------------------------------
def _evaluate_write(
    node: CallNode, env: Env, program: Program
) -> WriteEvaluation | None:
    if not node.is_write:
        return None
    target = node.target
    operation = node.operation

    def make(
        reason: str,
        detail: str,
        oid: str | None,
        version: int | None,
        matched: FactKey | None = None,
        origins: frozenset[str] | None = None,
        evidence: str | None = None,
    ) -> WriteEvaluation:
        return WriteEvaluation(
            node_id=node.node_id,
            target=target,
            object_id=oid,
            version=version,
            operation=operation,
            authorized=False,
            reason=reason,
            detail=detail,
            matched_fact=matched,
            fact_origins=origins or frozenset(),
            evidence_node_id=evidence,
        )

    if target is None:
        return make(
            REASON_UNBOUND,
            "写入调用没有声明目标变量，没有可承载授权事实的对象",
            None,
            None,
        )
    # 合流发散优先于未绑定：变量在不同路径上绑定不一致时，合流后绑定被
    # 移除但 unknown 标记保留了"曾经发散"的信息。
    if target in env.unknown_variables:
        evidence = _latest_gap_for(env, variable=target)
        # 同一对象只是版本不同 → 对象级发散；绑定到不同对象 → 变量级发散
        gap = env.gaps[-1] if env.gaps else None
        if (
            gap is not None
            and target in gap.variable_diffs
            and gap.object_version_diffs
        ):
            oid_views = {
                view[0]
                for view in gap.variable_diffs[target]
                if view is not None
            }
            if len(oid_views) == 1:
                return make(
                    REASON_DIVERGENT_OBJECT,
                    f"对象 {next(iter(oid_views))!r} 的版本在不同路径上不一致，"
                    "合流后无法确认授权所系版本",
                    next(iter(oid_views)),
                    None,
                    evidence=evidence,
                )
        return make(
            REASON_DIVERGENT_VARIABLE,
            f"写入目标 {target!r} 在不同路径上绑定不一致，合流后无法确定对象",
            None,
            None,
            evidence=evidence,
        )
    if target not in env.bindings:
        return make(
            REASON_UNBOUND,
            f"写入目标 {target!r} 无有效对象绑定，没有可承载授权事实的对象",
            None,
            None,
        )
    oid, version = env.bindings[target]
    if oid in env.unknown_objects:
        evidence = _latest_gap_for(env, object_id=oid)
        return make(
            REASON_DIVERGENT_OBJECT,
            f"对象 {oid!r} 的版本在不同路径上不一致，合流后无法确认授权所系版本",
            oid,
            version,
            evidence=evidence,
        )

    # 副本判定：当前绑定由副本节点建立
    origin_id = env.binding_origins.get(target)
    origin_node = (
        program.nodes.get(origin_id) if origin_id is not None else None
    )
    if isinstance(origin_node, AliasNode) and origin_node.is_copy:
        return make(
            REASON_OBJECT_COPY,
            f"写入目标 {target!r} 指向对象副本 {oid!r}；授权事实绑定在原对象上，"
            "副本不继承授权",
            oid,
            version,
            evidence=origin_id,
        )

    exact = FactKey(oid, version, operation) if operation is not None else None
    if exact is not None and exact in env.facts:
        return WriteEvaluation(
            node_id=node.node_id,
            target=target,
            object_id=oid,
            version=version,
            operation=operation,
            authorized=True,
            reason=REASON_AUTHORIZED,
            detail=(
                f"写入受授权事实约束：对象 {oid!r} 版本 {version} "
                f"操作 {operation!r}，来源检查 {sorted(env.facts[exact])}"
            ),
            matched_fact=exact,
            fact_origins=env.facts[exact],
            evidence_node_id=None,
        )

    # 同对象同版本、不同操作
    if operation is not None:
        for key, origins in env.facts.items():
            if key.object_id == oid and key.version == version:
                return make(
                    REASON_OPERATION_MISMATCH,
                    f"对象 {oid!r} 版本 {version} 上存在操作 {key.operation!r} "
                    f"的授权，但写入要求操作 {operation!r}",
                    oid,
                    version,
                    matched=key,
                    origins=origins,
                )
    # 同对象、旧版本（版本已被推进/替换）。存活事实与已失效事实都要考虑：
    # 推进发生后旧事实已从 facts 移入 invalidated，依赖链仍然从该事实的
    # 授权检查开始，断在推进/替换节点。
    stale_candidates = sorted(
        (
            (key, origins)
            for key, origins in env.facts.items()
            if key.object_id == oid
        ),
        key=lambda item: (item[0].version, item[0].operation),
    )
    invalidated_hits = [
        item
        for item in reversed(env.invalidated)
        if item.fact.object_id == oid
    ]
    if stale_candidates:
        key, origins = stale_candidates[0]
        advance_node = _latest_history_node(env, oid)
        return make(
            REASON_STALE_VERSION,
            f"授权事实绑定在 {oid!r} 版本 {key.version}，而写入目标已是版本 "
            f"{version}；对象版本已被推进/替换，旧授权不覆盖新版本",
            oid,
            version,
            matched=key,
            origins=origins,
            evidence=advance_node,
        )
    if invalidated_hits:
        item = invalidated_hits[0]
        if item.reason == REASON_TX_ROLLBACK:
            return make(
                REASON_TX_ROLLBACK,
                f"对象 {oid!r} 上的授权事实在事务回滚节点 {item.node_id!r} "
                "失效，回滚后写入路径上没有有效授权",
                oid,
                version,
                matched=item.fact,
                origins=item.origins,
                evidence=item.node_id,
            )
        advance_node = _latest_history_node(env, oid) or item.node_id
        return make(
            REASON_STALE_VERSION,
            f"授权事实绑定在 {oid!r} 版本 {item.fact.version}，对象随后被"
            "推进/替换，旧授权不覆盖当前版本 "
            f"{version}（失效原因 {item.reason}）",
            oid,
            version,
            matched=item.fact,
            origins=item.origins,
            evidence=advance_node,
        )

    # 绑定由调用摘要重绑定而来，且环境中不存在任何同对象（含已失效）事实
    # → 外部来源覆盖，原依赖链被切断
    if isinstance(origin_node, CallNode):
        return make(
            REASON_OVERWRITTEN_BINDING,
            f"写入目标 {target!r} 的绑定被调用 {origin_id!r} 的摘要重绑定到 "
            f"{oid!r} 版本 {version}，该来源未携带授权事实，原依赖链被覆盖",
            oid,
            version,
            evidence=origin_id,
        )

    return make(
        REASON_NO_AUTHORIZATION,
        f"到达写入 {node.node_id!r} 的路径上不存在对象 {oid!r} 版本 {version} "
        f"操作 {operation!r} 的成功授权事实",
        oid,
        version,
    )


def _latest_gap_for(
    env: Env,
    *,
    variable: str | None = None,
    object_id: str | None = None,
) -> str | None:
    for gap in reversed(env.gaps):
        if variable is not None and (
            variable in gap.variable_diffs
            or variable in gap.unknown_variables
        ):
            return gap.merge_node_id
        if object_id is not None and (
            object_id in gap.object_version_diffs
            or object_id in gap.unknown_objects
        ):
            return gap.merge_node_id
    return env.gaps[-1].merge_node_id if env.gaps else None


def _latest_history_node(env: Env, object_id: str) -> str | None:
    for event in reversed(env.history):
        if event.object_id == object_id:
            return event.node_id
    return None


# --------------------------------------------------------------------------
# 调用摘要的持久化效果
# --------------------------------------------------------------------------
def _apply_call_summary(node: CallNode, env: Env) -> None:
    # 1) 整对象替换：该对象所有版本上的事实失效
    for oid in sorted(node.replaces):
        removed = [
            (key, origins)
            for key, origins in env.facts.items()
            if key.object_id == oid
        ]
        for key, origins in removed:
            del env.facts[key]
            env.invalidated.append(
                InvalidatedFact(
                    fact=key,
                    origins=origins,
                    reason=REASON_OBJECT_REPLACED,
                    node_id=node.node_id,
                    detail=f"调用 {node.node_id!r} 替换了对象 {oid!r}",
                )
            )
        env.history.append(
            VersionEvent(
                node_id=node.node_id,
                object_id=oid,
                old_version=None,
                new_version=None,
                reason=REASON_OBJECT_REPLACED,
                detail=f"对象 {oid!r} 被整对象替换",
            )
        )

    # 2) 版本推进：旧版本上的事实失效
    for oid, new_ver in sorted(node.advances.items()):
        old_versions = sorted(
            {
                key.version
                for key in env.facts
                if key.object_id == oid and key.version != new_ver
            }
        )
        removed = [
            (key, origins)
            for key, origins in list(env.facts.items())
            if key.object_id == oid and key.version != new_ver
        ]
        for key, origins in removed:
            del env.facts[key]
            env.invalidated.append(
                InvalidatedFact(
                    fact=key,
                    origins=origins,
                    reason=REASON_VERSION_ADVANCED,
                    node_id=node.node_id,
                    detail=(
                        f"调用 {node.node_id!r} 将对象 {oid!r} 推进到版本 "
                        f"{new_ver}，版本 {key.version} 上的授权失效"
                    ),
                )
            )
        env.history.append(
            VersionEvent(
                node_id=node.node_id,
                object_id=oid,
                old_version=old_versions[-1] if old_versions else None,
                new_version=new_ver,
                reason=REASON_VERSION_ADVANCED,
            )
        )

    # 3) 重绑定 / 解绑
    for var, binding in sorted(node.rebindings.items()):
        old = env.bindings.get(var)
        env.bindings[var] = binding
        env.binding_origins[var] = node.node_id
        env.unknown_variables.discard(var)
        env.binding_events.append(
            BindingEvent(
                node_id=node.node_id,
                variable=var,
                old_binding=old,
                new_binding=binding,
                reason="rebinding",
                detail=(
                    f"调用 {node.node_id!r} 将 {var!r} 重绑定到 {binding}"
                ),
            )
        )
    for var in sorted(node.unbinds):
        old = env.bindings.pop(var, None)
        env.binding_origins.pop(var, None)
        env.unknown_variables.add(var)
        env.binding_events.append(
            BindingEvent(
                node_id=node.node_id,
                variable=var,
                old_binding=old,
                new_binding=None,
                reason=REASON_UNBOUND,
            )
        )

# --------------------------------------------------------------------------
# 事务回滚
# --------------------------------------------------------------------------
def _apply_rollback(node: TxRollbackNode, env: Env) -> None:
    if not env.tx_stack:
        raise ProgramStructureError(
            f"回滚节点 {node.node_id!r} 没有匹配的事务开始节点"
        )
    snapshot = env.tx_stack.pop()
    current_facts = dict(env.facts)

    # 事务内新增、或来源集合被改写过的事实全部失效
    for key, origins in sorted(
        current_facts.items(),
        key=lambda item: (item[0].object_id, item[0].version, item[0].operation),
    ):
        before = snapshot.facts.get(key)
        if before is None:
            env.invalidated.append(
                InvalidatedFact(
                    fact=key,
                    origins=origins,
                    reason=REASON_TX_ROLLBACK,
                    node_id=node.node_id,
                    detail="事务内产生的授权事实随回滚失效",
                )
            )
        elif before != origins:
            env.invalidated.append(
                InvalidatedFact(
                    fact=key,
                    origins=origins - before,
                    reason=REASON_TX_ROLLBACK,
                    node_id=node.node_id,
                    detail="事务内传播扩展的事实来源随回滚失效",
                )
            )

    env.bindings = dict(snapshot.bindings)
    env.binding_origins = dict(snapshot.binding_origins)
    env.facts = {k: frozenset(v) for k, v in snapshot.facts.items()}
    env.unknown_objects = set(snapshot.unknown_objects)
    env.unknown_variables = set(snapshot.unknown_variables)
    env.history.append(
        VersionEvent(
            node_id=node.node_id,
            object_id="",
            old_version=None,
            new_version=None,
            reason=REASON_TX_ROLLBACK,
            detail="事务回滚，绑定与授权事实恢复到事务开始快照",
        )
    )


# --------------------------------------------------------------------------
# 断链重建
# --------------------------------------------------------------------------
def _build_finding(program: Program, ev: WriteEvaluation) -> Finding:
    chain = _build_chain(program, ev)
    return Finding(
        node_id=ev.node_id,
        target=ev.target,
        object_id=ev.object_id,
        version=ev.version,
        operation=ev.operation,
        reason=ev.reason,
        detail=ev.detail,
        chain=tuple(chain),
        matched_fact=ev.matched_fact,
        fact_origins=ev.fact_origins,
    )


def _role_of(node: Node) -> str:
    if isinstance(node, AuthCheckNode):
        return "auth_check"
    if isinstance(node, AliasNode):
        return "copy" if node.is_copy else "alias"
    if isinstance(node, CallNode):
        return "call"
    if isinstance(node, TxBeginNode):
        return "tx_begin"
    if isinstance(node, TxCommitNode):
        return "tx_commit"
    if isinstance(node, TxRollbackNode):
        return "rollback"
    if isinstance(node, BranchNode):
        return "branch"
    if isinstance(node, MergeNode):
        return "merge"
    if isinstance(node, EntryNode):
        return "entry"
    return node.kind


def _shortest_path(
    program: Program, src: str | None, dst: str
) -> list[str]:
    """CFG 上 src -> dst 的最短节点序列；src 为 None 时返回 [dst]。"""
    if src is None or src == dst:
        return [dst]
    queue: list[tuple[str, list[str]]] = [(src, [src])]
    seen = {src}
    while queue:
        current, path = queue.pop(0)
        for nxt, _label in program.successors_of(current):
            if nxt in seen:
                continue
            new_path = [*path, nxt]
            if nxt == dst:
                return new_path
            seen.add(nxt)
            queue.append((nxt, new_path))
    # 不可达（理论上不会发生，两个节点均从入口可达且图为 DAG）
    return [dst]


def _build_chain(
    program: Program, ev: WriteEvaluation
) -> list[ChainLink]:
    write_id = ev.node_id
    anchor: str | None = None
    if ev.fact_origins:
        anchor = sorted(ev.fact_origins)[0]
    elif ev.evidence_node_id is not None:
        anchor = ev.evidence_node_id

    path = _shortest_path(program, anchor, write_id)
    links: list[ChainLink] = []
    for nid in path:
        node = program.nodes[nid]
        role = _role_of(node)
        is_break = False
        note = ""
        if nid == write_id:
            is_break = True
            role = "unauthorized_write"
            note = f"写入位置：{ev.reason}"
        elif ev.reason == REASON_STALE_VERSION and isinstance(node, CallNode):
            if (
                ev.object_id in node.replaces
                or ev.object_id in node.advances
            ):
                is_break = True
                role = "version_advance"
                note = "对象在此被替换/推进到新版本，旧授权事实失效"
        elif ev.reason == REASON_OBJECT_COPY and isinstance(node, AliasNode):
            if node.is_copy:
                is_break = True
                role = "copy"
                note = "对象副本不继承原对象的授权事实"
        elif ev.reason == REASON_OVERWRITTEN_BINDING and isinstance(
            node, CallNode
        ):
            if nid == ev.evidence_node_id:
                is_break = True
                role = "replacement"
                note = "变量绑定被未经授权的外部来源覆盖"
        elif ev.reason in (
            REASON_DIVERGENT_OBJECT,
            REASON_DIVERGENT_VARIABLE,
        ) and isinstance(node, (MergeNode,)):
            if nid == ev.evidence_node_id:
                is_break = True
                role = "merge"
                note = "路径合流时绑定/版本/事实不一致，must 事实丢失"
        elif (
            ev.reason in (REASON_DIVERGENT_OBJECT, REASON_DIVERGENT_VARIABLE)
            and isinstance(node, CallNode)
            and nid == ev.evidence_node_id
        ):
            is_break = True
            role = "merge"
            note = "多前驱调用节点上合流丢失 must 事实"
        elif ev.reason == REASON_TX_ROLLBACK and isinstance(
            node, TxRollbackNode
        ):
            is_break = True
            note = "事务回滚，事务内授权事实失效"
        links.append(
            ChainLink(
                node_id=nid,
                node_kind=node.kind,
                role=role,
                is_break=is_break,
                note=note,
            )
        )

    # 多前驱调用节点本身承担合流角色时的兜底标注
    if ev.evidence_node_id == write_id and not any(
        link.is_break for link in links
    ):
        links[-1] = ChainLink(
            node_id=write_id,
            node_kind=program.nodes[write_id].kind,
            role="merge",
            is_break=True,
            note="多前驱合流后无法证明授权",
        )
    return links


# --------------------------------------------------------------------------
# 去重工具
# --------------------------------------------------------------------------
def _dedupe_invalidated(
    items: Iterable[InvalidatedFact],
) -> list[InvalidatedFact]:
    seen: set[tuple] = set()
    out: list[InvalidatedFact] = []
    for item in items:
        key = (
            item.fact.object_id,
            item.fact.version,
            item.fact.operation,
            item.reason,
            item.node_id,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    out.sort(key=lambda x: (x.node_id, x.reason, x.fact.object_id,
                            x.fact.version, x.fact.operation))
    return out


def _dedupe_gaps(gaps: Iterable[MergeGap]) -> list[MergeGap]:
    seen: set[str] = set()
    out: list[MergeGap] = []
    for gap in gaps:
        if gap.merge_node_id in seen:
            continue
        seen.add(gap.merge_node_id)
        out.append(gap)
    return sorted(out, key=lambda g: g.merge_node_id)
