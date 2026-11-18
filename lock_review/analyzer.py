"""核心分析：从动作流推导锁等待环，并按配置/互斥/读写约束过滤。

判定规则（与题面交叉约束一一对应）：

1. 动作流展开为"持有-等待"边：申请锁 B 时仍持有锁 A，则记边 A -> B，
   并记录两侧锁的读写模式、所属动作流、处置阶段与启用条件。
2. 取消路径（cancel）释放锁后该路径终止，但其余路径继续参与检查，
   不因单条取消路径而漏报其他可能路径。
3. 等待环仅在"同一配置组合里可并发出现"时成立：环上所有动作流的
   启用条件须同时被某个支持的配置组合满足。
4. 互斥运维模式组内不同模式的动作流不会并发，其逆序获取不报。
5. 环上每个锁的持有/等待两侧至少有一方为写访问；同一入口读锁共存
   （读-读）不算冲突。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Any, Dict, List, Sequence, Tuple

from .model import Spec, Stream, load_spec_file


@dataclass(frozen=True)
class HoldEdge:
    """持有-等待边：某动作流持有 ``holder`` 锁时申请 ``waiter`` 锁。"""

    holder: str
    waiter: str
    stream: str
    phase: str
    mode: str
    conditions: frozenset
    hold_mode: str  # 持有侧锁的访问模式 read/write
    wait_mode: str  # 等待侧锁的访问模式 read/write


def _stream_edges(stream: Stream) -> List[HoldEdge]:
    """沿动作流的各条路径收集持有-等待边。

    路径用"当前持有锁 -> 访问模式"的字典表示。cancel 步骤派生一条
    释放指定锁后即终止的取消路径；未取消的路径继续向后检查。
    """
    edges: List[HoldEdge] = []
    active: List[Dict[str, str]] = [dict()]
    for step in stream.steps:
        op = step["op"]
        if op == "acquire":
            lock = step["lock"]
            access = step.get("mode", "write")
            for held in active:
                for h_lock, h_mode in list(held.items()):
                    edges.append(HoldEdge(
                        holder=h_lock,
                        waiter=lock,
                        stream=stream.id,
                        phase=stream.phase,
                        mode=stream.mode,
                        conditions=stream.conditions,
                        hold_mode=h_mode,
                        wait_mode=access,
                    ))
                held[lock] = access
        elif op == "release":
            for held in active:
                held.pop(step["lock"], None)
        elif op == "cancel":
            # 取消路径：释放指定锁并终止（此前已记录的边属于取消前的
            # 公共前缀，仍然有效）；其余路径保持不变、继续检查。
            for held in active:
                cancelled = dict(held)
                for lock in step.get("releases", []):
                    cancelled.pop(lock, None)
                # cancelled 路径到此结束，不再产生新的持有-等待边。
    return edges


def _find_cycles(adj: Dict[str, Tuple[str, ...]]) -> List[Tuple[str, ...]]:
    """在锁等待图中枚举简单环（每个环以最小节点为起点，去重）。"""
    cycles: List[Tuple[str, ...]] = []
    for start in sorted(adj):
        stack: List[Tuple[str, List[str]]] = [(start, [start])]
        while stack:
            node, path = stack.pop()
            for nxt in adj.get(node, ()):
                if nxt == start and len(path) > 1:
                    cycles.append(tuple(path))
                elif nxt > start and nxt not in path:
                    stack.append((nxt, path + [nxt]))
    return cycles


def _modes_exclusive(groups: Sequence[frozenset], mode_a: str, mode_b: str) -> bool:
    """两个不同模式是否属于同一互斥运维模式组。"""
    if mode_a == mode_b:
        return False
    return any(mode_a in g and mode_b in g for g in groups)


def _cycle_witnesses(
    spec: Spec,
    cycle: Tuple[str, ...],
    edges_by_pair: Dict[Tuple[str, str], List[HoldEdge]],
) -> List[Dict[str, Any]]:
    """为等待环寻找可行见证：同一配置可并发、模式不互斥、读写冲突。"""
    n = len(cycle)
    pair_edges: List[List[HoldEdge]] = []
    for i in range(n):
        pair = (cycle[i], cycle[(i + 1) % n])
        options = edges_by_pair.get(pair)
        if not options:
            return []
        pair_edges.append(options)

    witnesses: List[Dict[str, Any]] = []
    for combo in product(*pair_edges):
        streams = {e.stream for e in combo}
        modes = {e.stream: e.mode for e in combo}
        # 约束 4：互斥运维模式之间不并发。
        stream_list = sorted(streams)
        if any(
            _modes_exclusive(spec.exclusive_groups, modes[a], modes[b])
            for idx, a in enumerate(stream_list)
            for b in stream_list[idx + 1:]
        ):
            continue
        # 约束 3：存在同一配置组合同时满足所有动作流的启用条件。
        common = [
            cfg.id for cfg in spec.configs
            if all(e.conditions <= cfg.enables for e in combo)
        ]
        if not common:
            continue
        # 约束 5：环上每个锁的持有/等待两侧至少一方为写。
        ok = True
        for i, lock in enumerate(cycle):
            hold_access = combo[i].hold_mode            # 持有该锁的一侧
            wait_access = combo[(i - 1) % n].wait_mode  # 等待该锁的一侧
            if "write" not in (hold_access, wait_access):
                ok = False  # 读-读共存，不算冲突
                break
        if not ok:
            continue
        witnesses.append({
            "locks": list(cycle),
            "configs": common,
            "streams": stream_list,
            "phases": sorted({e.phase for e in combo}),
            "holds": [
                {
                    "stream": e.stream,
                    "phase": e.phase,
                    "mode": e.mode,
                    "holds": e.holder,
                    "hold_access": e.hold_mode,
                    "waits_for": e.waiter,
                    "wait_access": e.wait_mode,
                }
                for e in combo
            ],
        })
    return witnesses


def review(spec: Spec) -> Dict[str, Any]:
    """审查规范，返回可能阻断处置的锁等待环报告。"""
    edges: List[HoldEdge] = []
    for stream in spec.streams:
        edges.extend(_stream_edges(stream))

    adj: Dict[str, set] = {}
    edges_by_pair: Dict[Tuple[str, str], List[HoldEdge]] = {}
    for e in edges:
        adj.setdefault(e.holder, set()).add(e.waiter)
        edges_by_pair.setdefault((e.holder, e.waiter), []).append(e)
    adjacency = {node: tuple(sorted(targets)) for node, targets in adj.items()}

    findings: List[Dict[str, Any]] = []
    for cycle in _find_cycles(adjacency):
        witnesses = _cycle_witnesses(spec, cycle, edges_by_pair)
        findings.extend(witnesses)

    findings.sort(key=lambda f: (f["locks"], f["streams"]))
    return {
        "findings": findings,
        "stats": {
            "streams": len(spec.streams),
            "configs": len(spec.configs),
            "hold_edges": len(edges),
            "cycles_checked": len(_find_cycles(adjacency)),
            "findings": len(findings),
        },
    }


def review_file(path: str) -> Dict[str, Any]:
    """从 JSON 规范文件执行审查。"""
    return review(load_spec_file(path))
