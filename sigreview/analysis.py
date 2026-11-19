"""路径敏感的信任证明数据流分析。

抽象状态
========
每个可达路径用一个不可变的 :class:`AbstractEnv` 表示（析构集合的元素）：

* ``vars``     : 变量当前指向的快照 id（重新赋值即覆盖，旧证明随之失效）
* ``bindings`` : 任务编号 -> 创建该任务时变量所持有的快照 id（编号只绑一次）
* ``truths``   : 任务编号 -> 经 ``branch`` 约束后已知的返回值真/假
* ``trace``    : 有界的分支经过记录，仅用于证据不足时定位路径

快照身份采用 **snapshot 赋值节点 id**：同名制品换版会产生新的快照 id，
旧任务绑定的仍是旧快照，无法为新快照背书。

等待节点（wait_all / wait_any）只做同步，传递函数是恒等函数——
等待本身不产生任何信任，只有 branch 对成功返回值的约束才算证明。

有限性
======
变量、快照、任务编号均来自有限图；分支轨迹按 ``trace_bound`` 截断，
因此环境总数有限，worklist 迭代必然到达不动点（循环按有限快照与
任务编号求稳定结论）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .model import BRANCH, PUBLISH, SNAPSHOT, VERIFY, WAIT_ALL, WAIT_ANY, CFG, Node

# finding 的原因码
REBIND_STATIC = "task_rebound"          # 同一任务编号在两个 verify 节点创建
REBIND_EXEC = "task_rebound_execution"  # 循环中同一 verify 节点被重复执行
UNBOUND_TASK = "unbound_task"           # branch/wait 引用了从未创建的任务
UNINIT_VAR = "uninitialized_variable"   # verify/publish 使用了未赋值变量

# 发布点证据不足的原因码
GAP_PROOF_MISSING = "proof_missing"                 # 该快照没有成功证明
GAP_UNINITIALIZED = "uninitialized_variable"        # 发布变量未赋值
GAP_PROOF_FAILED = "proof_failed"                   # 仅有的证明返回假


@dataclass(frozen=True)
class AbstractEnv:
    """单条路径的抽象环境（不可变、可哈希，可放入集合求不动点）。"""

    vars: tuple = ()
    bindings: tuple = ()
    truths: tuple = ()
    trace: tuple = ()

    def var_snapshot(self, var: str) -> Optional[str]:
        return dict(self.vars).get(var)

    def binding(self, task_id: str) -> Optional[str]:
        return dict(self.bindings).get(task_id)

    def truth(self, task_id: str) -> Optional[bool]:
        return dict(self.truths).get(task_id)

    def with_var(self, var: str, snapshot_id: str) -> "AbstractEnv":
        kept = tuple((v, s) for v, s in self.vars if v != var)
        return AbstractEnv(kept + ((var, snapshot_id),), self.bindings,
                           self.truths, self.trace)

    def with_binding(self, task_id: str, snapshot_id: str) -> "AbstractEnv":
        return AbstractEnv(self.vars,
                           self.bindings + ((task_id, snapshot_id),),
                           self.truths, self.trace)

    def with_truth(self, task_id: str, value: bool, node_id: str,
                   bound: int) -> "AbstractEnv":
        kept = tuple((t, v) for t, v in self.truths if t != task_id)
        trace = self.trace
        if len(trace) < bound:
            trace = trace + ((node_id, "T" if value else "F"),)
        return AbstractEnv(self.vars, self.bindings,
                           kept + ((task_id, value),), trace)


@dataclass
class PublishReport:
    """单个发布点的审查结论。"""

    node_id: str
    var: str
    passed: bool
    evidence: List[dict] = field(default_factory=list)
    gaps: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "var": self.var,
            "passed": self.passed,
            "evidence": self.evidence,
            "gaps": self.gaps,
        }


@dataclass
class ReviewResult:
    publish_reports: List[PublishReport]
    findings: List[dict]
    iterations: int

    @property
    def blocked(self) -> bool:
        """存在结构性违规或任一发布点证据不足即阻断。"""
        return bool(self.findings) or any(
            not r.passed for r in self.publish_reports
        )

    @property
    def ok(self) -> bool:
        return not self.blocked

    def to_dict(self) -> dict:
        return {
            "blocked": self.blocked,
            "iterations": self.iterations,
            "findings": self.findings,
            "publish_reports": [r.to_dict() for r in self.publish_reports],
        }


def _static_checks(cfg: CFG):
    """全局结构检查：任务编号单次绑定、被引用任务均已创建。

    返回 (findings, 任务->verify节点 映射)。
    """
    findings: List[dict] = []
    task_origin: Dict[str, str] = {}
    for node in cfg.nodes.values():
        if node.type == VERIFY:
            if node.task_id in task_origin:
                findings.append({
                    "rule": REBIND_STATIC,
                    "node_id": node.id,
                    "task_id": node.task_id,
                    "first_bound_at": task_origin[node.task_id],
                    "message": (
                        f"任务编号 {node.task_id!r} 已在节点 "
                        f"{task_origin[node.task_id]!r} 绑定，不能再次绑定"
                    ),
                })
            else:
                task_origin[node.task_id] = node.id

    created = set(task_origin)
    for node in cfg.nodes.values():
        refs: List[str] = []
        if node.type in (WAIT_ALL, WAIT_ANY):
            refs = list(node.task_ids)
        elif node.type == BRANCH:
            refs = [node.task_id]
        for task_id in refs:
            if task_id not in created:
                findings.append({
                    "rule": UNBOUND_TASK,
                    "node_id": node.id,
                    "task_id": task_id,
                    "message": (
                        f"节点 {node.id!r} 引用了从未创建的验签任务 "
                        f"{task_id!r}"
                    ),
                })
    return findings, task_origin


def analyze_graph(cfg: CFG, trace_bound: int = 8) -> ReviewResult:
    """对 CFG 运行信任证明分析，返回所有发布点的结论与结构性问题。"""
    if trace_bound < 0:
        raise ValueError("trace_bound 必须是非负整数")

    findings, task_origin = _static_checks(cfg)
    snapshots: Dict[str, Node] = {
        n.id: n for n in cfg.nodes.values() if n.type == SNAPSHOT
    }

    # incoming[node] = 到达该节点入口的抽象环境集合（析取）
    incoming: Dict[str, set] = {nid: set() for nid in cfg.nodes}
    incoming[cfg.entry].add(AbstractEnv())

    worklist: deque = deque([cfg.entry])
    on_queue = {cfg.entry}
    iterations = 0

    def push(succ_id: str, env: AbstractEnv) -> None:
        bucket = incoming[succ_id]
        if env not in bucket:
            bucket.add(env)
            if succ_id not in on_queue:
                on_queue.add(succ_id)
                worklist.append(succ_id)

    while worklist:
        nid = worklist.popleft()
        on_queue.discard(nid)
        node = cfg.nodes[nid]
        iterations += 1

        for env in list(incoming[nid]):
            if node.type == SNAPSHOT:
                # 重新赋值产生全新快照身份，旧任务证明无法沿用。
                for succ in node.successors():
                    push(succ, env.with_var(node.var, node.id))

            elif node.type == VERIFY:
                snap_id = env.var_snapshot(node.var)
                if snap_id is None:
                    findings.append({
                        "rule": UNINIT_VAR,
                        "node_id": node.id,
                        "var": node.var,
                        "message": (
                            f"节点 {node.id!r} 对未赋值变量 "
                            f"{node.var!r} 创建验签任务"
                        ),
                    })
                    continue  # 该路径无意义，不再传播
                if env.binding(node.task_id) is not None:
                    # 循环重入：编号只能绑定一次，保留首次绑定并记录。
                    findings.append({
                        "rule": REBIND_EXEC,
                        "node_id": node.id,
                        "task_id": node.task_id,
                        "message": (
                            f"任务编号 {node.task_id!r} 在节点 {node.id!r} "
                            f"被重复绑定，沿用首次绑定的快照"
                        ),
                    })
                else:
                    env = env.with_binding(node.task_id, snap_id)
                for succ in node.successors():
                    push(succ, env)

            elif node.type in (WAIT_ALL, WAIT_ANY):
                # 同步屏障：只保证任务结束，不赋予任何信任，状态原样传播。
                for succ in node.successors():
                    push(succ, env)

            elif node.type == BRANCH:
                known = env.truth(node.task_id)
                branches = (
                    ((node.true_next, True),) if known is True
                    else ((node.false_next, False),) if known is False
                    else ((node.true_next, True), (node.false_next, False))
                )
                for succ, value in branches:
                    if succ is None:
                        continue
                    push(succ, env.with_truth(
                        node.task_id, value, node.id, trace_bound
                    ))

            elif node.type == PUBLISH:
                # 发布点在不动点计算中不改变状态，继续向后传播。
                for succ in node.successors():
                    push(succ, env)

    reports = [
        _evaluate_publish(node, incoming[node.id], task_origin, snapshots)
        for node in cfg.nodes.values()
        if node.type == PUBLISH
    ]
    return ReviewResult(
        publish_reports=reports,
        findings=_dedup_findings(findings),
        iterations=iterations,
    )


def _dedup_findings(findings: List[dict]) -> List[dict]:
    seen = set()
    out = []
    for item in findings:
        key = tuple(sorted(item.items()))
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _evaluate_publish(
    node: Node,
    envs: set,
    task_origin: Dict[str, str],
    snapshots: Dict[str, Node],
) -> PublishReport:
    """判断发布点的所有可达环境是否都持有实际快照的成功证明。"""
    evidence: List[dict] = []
    gaps: List[dict] = []
    seen_evidence = set()

    for env in sorted(envs, key=lambda e: e.trace):
        snap_id = env.var_snapshot(node.var)
        if snap_id is None:
            gaps.append({
                "reason": GAP_UNINITIALIZED,
                "node_id": node.id,
                "var": node.var,
                "trace": list(env.trace),
                "message": f"发布变量 {node.var!r} 在该路径上未赋值",
            })
            continue

        snap_node = snapshots.get(snap_id)
        proofs = [
            t for t, s in env.bindings
            if s == snap_id and env.truth(t) is True
        ]
        if not proofs:
            # 列出与该环境相关的任务，帮助定位“证明丢失对应关系/已过期”。
            related = []
            for t, s in env.bindings:
                related.append({
                    "task_id": t,
                    "verify_node": task_origin.get(t),
                    "bound_snapshot": _snapshot_info(s, snapshots.get(s)),
                    "truth": env.truth(t),
                    "stale": s != snap_id,
                })
            failed_here = [
                r for r in related
                if not r["stale"] and r["truth"] is False
            ]
            reason = GAP_PROOF_FAILED if failed_here else GAP_PROOF_MISSING
            gaps.append({
                "reason": reason,
                "node_id": node.id,
                "var": node.var,
                "snapshot": _snapshot_info(snap_id, snap_node),
                "related_tasks": related,
                "trace": list(env.trace),
                "message": _gap_message(reason, node.var, snap_node),
            })
            continue

        for task_id in proofs:
            if task_id in seen_evidence:
                continue
            seen_evidence.add(task_id)
            evidence.append({
                "task_id": task_id,
                "verify_node": task_origin.get(task_id),
                "snapshot": _snapshot_info(snap_id, snap_node),
            })

    return PublishReport(
        node_id=node.id,
        var=node.var,
        passed=not gaps and bool(envs),
        evidence=evidence,
        gaps=gaps,
    )


def _snapshot_info(snap_id: str, snap_node: Optional[Node]) -> dict:
    if snap_node is None:
        return {"snapshot_id": snap_id}
    return {
        "snapshot_id": snap_id,
        "artifact": snap_node.artifact,
        "version": snap_node.version,
        "assigned_at": snap_node.id,
    }


def _gap_message(reason: str, var: str, snap_node: Optional[Node]) -> str:
    if reason == GAP_PROOF_FAILED:
        return f"变量 {var!r} 的验签任务返回假，证明失败"
    if snap_node is not None:
        return (
            f"缺少对快照 {snap_node.artifact!r}@{snap_node.version!r}"
            f"（节点 {snap_node.id!r}）的成功验签证明"
        )
    return f"变量 {var!r} 缺少成功验签证明"
