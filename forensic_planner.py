"""取证采集规划模块.

输入:
  - 证据类别(是否必取、是否需要原始状态)
  - 采集方式(耗时、覆盖的类别、先决条件、会改变的原始证据)
  - 总时限

输出:
  - 可行时: 一个满足全部交叉约束的执行序列(单次采集可覆盖多种材料,
    需要原始状态的材料排在可能改变它的动作之前, 总耗时不超过时限);
  - 不可行时: 指出相互阻塞的约束(排序环、自破坏、时限不足、无采集方式)。
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product
import heapq


# ---------------------------------------------------------------- 数据模型

@dataclass(frozen=True)
class EvidenceCategory:
    """一类待采集的证据材料."""

    id: str
    required: bool = True            # 是否必取
    needs_raw_state: bool = False    # 是否需要在可能改变它的动作之前被保存


@dataclass(frozen=True)
class AcquisitionMethod:
    """一种采集方式. 单次采集可覆盖多种材料."""

    id: str
    duration: int                    # 耗时(分钟), 必须 >= 0
    covers: frozenset                # 覆盖的证据类别 id
    prerequisites: frozenset = frozenset()   # 必须先执行的方式 id
    alters: frozenset = frozenset()          # 会改变其原始状态的类别 id

    def __init__(self, id, duration, covers, prerequisites=(), alters=()):
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "duration", duration)
        object.__setattr__(self, "covers", frozenset(covers))
        object.__setattr__(self, "prerequisites", frozenset(prerequisites))
        object.__setattr__(self, "alters", frozenset(alters))


@dataclass(frozen=True)
class ScheduledStep:
    """计划中的一个执行步骤."""

    method_id: str
    start: int
    end: int


@dataclass(frozen=True)
class Conflict:
    """一条相互阻塞/无法满足的约束描述."""

    kind: str                        # uncoverable_category / self_alter /
                                     # ordering_cycle / time_limit_exceeded
    detail: str
    categories: tuple = ()
    methods: tuple = ()


@dataclass(frozen=True)
class PlanResult:
    feasible: bool
    schedule: tuple = ()             # tuple[ScheduledStep], 按执行顺序
    covered: frozenset = frozenset()
    total_duration: int = 0
    time_limit: int = 0
    conflicts: tuple = ()            # tuple[Conflict], 仅不可行时非空


# ---------------------------------------------------------------- 输入校验

def _validate(categories, methods, time_limit):
    if time_limit < 0:
        raise ValueError("time_limit 必须 >= 0")
    cat_ids = [c.id for c in categories]
    if len(set(cat_ids)) != len(cat_ids):
        raise ValueError("证据类别 id 重复")
    method_ids = [m.id for m in methods]
    if len(set(method_ids)) != len(method_ids):
        raise ValueError("采集方式 id 重复")
    known_cats = set(cat_ids)
    known_methods = set(method_ids)
    for m in methods:
        if m.duration < 0:
            raise ValueError(f"采集方式 {m.id!r} 的耗时为负")
        unknown = m.covers - known_cats
        if unknown:
            raise ValueError(f"采集方式 {m.id!r} 覆盖了未知类别: {sorted(unknown)}")
        unknown = m.prerequisites - known_methods
        if unknown:
            raise ValueError(f"采集方式 {m.id!r} 引用了未知先决条件: {sorted(unknown)}")
        unknown = m.alters - known_cats
        if unknown:
            raise ValueError(f"采集方式 {m.id!r} 引用了未知被改类别: {sorted(unknown)}")


# ---------------------------------------------------------------- 求解器

def _closure(selected, by_id):
    """选定方式及其全部传递先决条件."""
    seen = set()
    stack = list(selected)
    while stack:
        mid = stack.pop()
        if mid in seen:
            continue
        seen.add(mid)
        stack.extend(by_id[mid].prerequisites)
    return seen


def _topo_order(nodes, edges):
    """Kahn 拓扑排序(字典序确定性). 有环时返回 None."""
    indegree = {n: 0 for n in nodes}
    adj = {n: [] for n in nodes}
    for a, b in edges:
        adj[a].append(b)
        indegree[b] += 1
    heap = [n for n in nodes if indegree[n] == 0]
    heapq.heapify(heap)
    order = []
    while heap:
        n = heapq.heappop(heap)
        order.append(n)
        for m in adj[n]:
            indegree[m] -= 1
            if indegree[m] == 0:
                heapq.heappush(heap, m)
    if len(order) != len(nodes):
        return None
    return order


def _find_cycle(nodes, edges):
    """返回环上的一个节点序列(用于冲突说明), 无环返回 None."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in nodes}
    adj = {n: [] for n in nodes}
    for a, b in edges:
        adj[a].append(b)
    stack = []

    def dfs(u):
        color[u] = GRAY
        stack.append(u)
        for v in adj[u]:
            if color[v] == GRAY:
                i = stack.index(v)
                return stack[i:] + [v]
            if color[v] == WHITE:
                r = dfs(v)
                if r is not None:
                    return r
        stack.pop()
        color[u] = BLACK
        return None

    for n in sorted(nodes):
        if color[n] == WHITE:
            r = dfs(n)
            if r is not None:
                return r
    return None


def plan_acquisition(categories, methods, time_limit):
    """制定采集计划.

    返回 PlanResult. feasible=True 时 schedule 给出一个满足全部约束的
    执行序列; 否则 conflicts 列出相互阻塞的约束.
    """
    categories = list(categories)
    methods = list(methods)
    _validate(categories, methods, time_limit)

    by_id = {m.id: m for m in methods}
    required = [c for c in categories if c.required]
    raw_required = {c.id for c in required if c.needs_raw_state}

    # 每个必取类别的候选采集方式(替代方案)
    candidates = {}
    conflicts = []
    for c in required:
        opts = sorted((m.id for m in methods if c.id in m.covers))
        if not opts:
            conflicts.append(Conflict(
                kind="uncoverable_category",
                detail=f"必取证据 {c.id!r} 没有任何采集方式覆盖",
                categories=(c.id,),
            ))
        candidates[c.id] = opts
    if conflicts:
        return PlanResult(feasible=False, time_limit=time_limit,
                          conflicts=tuple(conflicts))

    req_ids = [c.id for c in required]
    seen_sets = set()
    best = None                      # (total, order_tuple)
    slow_but_orderable = []          # 排序可行但超时的总耗时
    combo_conflicts = []             # 各组合失败原因

    choice_lists = [candidates[cid] for cid in req_ids]
    for combo in product(*choice_lists):
        chosen = dict(zip(req_ids, combo))
        key = frozenset(combo)
        if key in seen_sets:
            continue
        seen_sets.add(key)

        nodes = _closure(key, by_id)

        # 约束边: a -> b 表示 a 必须先于 b; 同时记录原因便于解释
        edges = set()
        reason = {}
        for mid in nodes:
            for p in by_id[mid].prerequisites:
                edges.add((p, mid))
                reason[(p, mid)] = f"{p!r} 是 {mid!r} 的先决条件"

        # 原始状态约束: 指定保存方式必须先于一切会改变该类别的动作
        self_alter = False
        for cid in raw_required:
            preserver = chosen[cid]
            if cid in by_id[preserver].alters:
                combo_conflicts.append(Conflict(
                    kind="self_alter",
                    detail=(f"采集方式 {preserver!r} 是类别 {cid!r} 的选定保存方式, "
                            f"但它本身会改变该类别的原始状态"),
                    categories=(cid,),
                    methods=(preserver,),
                ))
                self_alter = True
                break
            for mid in nodes:
                if mid != preserver and cid in by_id[mid].alters:
                    edges.add((preserver, mid))
                    reason[(preserver, mid)] = (
                        f"{cid!r} 需要原始状态, 必须由 {preserver!r} "
                        f"在会改变它的 {mid!r} 之前保存")
        if self_alter:
            continue

        order = _topo_order(nodes, edges)
        if order is None:
            cyc = _find_cycle(nodes, edges)
            why = "; ".join(reason.get((a, b), f"{a!r} -> {b!r}")
                            for a, b in zip(cyc, cyc[1:]))
            combo_conflicts.append(Conflict(
                kind="ordering_cycle",
                detail=f"排序约束成环 {' -> '.join(cyc)}: {why}",
                categories=tuple(sorted(raw_required)),
                methods=tuple(sorted(set(cyc))),
            ))
            continue

        total = sum(by_id[m].duration for m in nodes)
        if total > time_limit:
            slow_but_orderable.append(total)
            continue

        if best is None or (total, tuple(order)) < (best[0], tuple(best[1])):
            best = (total, order)

    if best is not None:
        total, order = best
        schedule = []
        t = 0
        for mid in order:
            d = by_id[mid].duration
            schedule.append(ScheduledStep(method_id=mid, start=t, end=t + d))
            t += d
        covered = frozenset().union(*(by_id[m].covers for m in order))
        return PlanResult(feasible=True, schedule=tuple(schedule),
                          covered=covered, total_duration=total,
                          time_limit=time_limit)

    # 汇总阻塞原因(去重)
    if slow_but_orderable:
        combo_conflicts.append(Conflict(
            kind="time_limit_exceeded",
            detail=(f"满足全部排序约束的方案最少耗时 "
                    f"{min(slow_but_orderable)} 分钟, 超过时限 {time_limit} 分钟"),
        ))
    uniq = {}
    for c in combo_conflicts:
        uniq.setdefault((c.kind, c.categories, c.methods, c.detail), c)
    return PlanResult(feasible=False, time_limit=time_limit,
                      conflicts=tuple(uniq.values()))


# ---------------------------------------------------------------- 演示入口

def _demo():
    cats = [
        EvidenceCategory("memory", required=True, needs_raw_state=True),
        EvidenceCategory("disk", required=True),
        EvidenceCategory("logs", required=True),
    ]
    methods = [
        AcquisitionMethod("mem_capture", 15, covers={"memory"}),
        AcquisitionMethod("disk_image", 90, covers={"disk"}),
        AcquisitionMethod("triage_tool", 20, covers={"logs"},
                          alters={"memory"}),
    ]
    result = plan_acquisition(cats, methods, time_limit=180)
    if result.feasible:
        print(f"计划可行, 总耗时 {result.total_duration}/{result.time_limit} 分钟:")
        for s in result.schedule:
            print(f"  [{s.start:>3}-{s.end:>3}] {s.method_id}")
    else:
        print("无可行计划, 阻塞约束:")
        for c in result.conflicts:
            print(f"  [{c.kind}] {c.detail}")


if __name__ == "__main__":
    _demo()
