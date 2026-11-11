"""约束求解与确定性最少移动规划。

约束模型：
1. 依赖边 depends_on：u -> v 表示 u 必须排在 v 前面（v.depends_on 含 u）。
2. 固定锚点 fixed：步骤必须落在指定槽位。
3. 同组约束在 parser 中已展开为相邻同组步骤间的依赖边。

规划流程（纯函数、确定性、不接触 DOM）：
  check_satisfiable -> 失败时给出具体冲突链
  build_target_order -> Kahn 拓扑排序，固定步骤优先，平局按原始下标
  plan_moves -> 加权 LCS（固定步骤权重更高，尽量不移动锚点）+
                按目标序反向插入，移动次数达到 n-LCS 理论下界。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .models import Spec


class UnsatisfiableError(Exception):
    """约束不可满足；chains 为人类可读的具体冲突链列表。"""

    def __init__(self, reason: str, chains: list[str]):
        super().__init__(reason)
        self.reason = reason
        self.chains = chains


@dataclass
class Move:
    """一次拖拽：把 step_id 拖到 anchor_id 之后（anchor 为 None 表示拖到首位）。"""

    step_id: str
    anchor_id: str | None
    note: str = ""


@dataclass
class Plan:
    target: list[str]
    moves: list[Move]
    lcs_size: int
    lower_bound: int          # n - LCS，任何重排方案的移动次数下界
    keep_fixed_untouched: bool
    diagnostics: list[str] = field(default_factory=list)

    @property
    def optimal(self) -> bool:
        return len(self.moves) == self.lower_bound


# --------------------------------------------------------------------------- #
# 图工具
# --------------------------------------------------------------------------- #
def build_graph(spec: Spec) -> tuple[dict[str, set[str]], dict[str, int]]:
    """返回 (后继邻接表 u->{v...}, 入度表)。"""
    succ: dict[str, set[str]] = {s.id: set() for s in spec.steps}
    indeg: dict[str, int] = {s.id: 0 for s in spec.steps}
    for vid, prereqs in spec.depends_on.items():
        for uid in prereqs:
            if vid not in succ[uid]:
                succ[uid].add(vid)
                indeg[vid] += 1
    return succ, indeg


def find_cycle(spec: Spec) -> list[str] | None:
    """DFS 找环，返回环上的节点 id 序列（首尾相同），无环返回 None。"""
    succ, _ = build_graph(spec)
    color: dict[str, int] = {s.id: 0 for s in spec.steps}  # 0 白 1 灰 2 黑
    stack: list[str] = []

    def dfs(u: str) -> list[str] | None:
        color[u] = 1
        stack.append(u)
        for v in sorted(succ[u]):
            if color[v] == 0:
                found = dfs(v)
                if found:
                    return found
            elif color[v] == 1:
                idx = stack.index(v)
                return stack[idx:] + [v]
        stack.pop()
        color[u] = 2
        return None

    for sid in spec.step_ids:
        if color[sid] == 0:
            found = dfs(sid)
            if found:
                return found
    return None


def _name(spec: Spec, sid: str) -> str:
    return f"{sid}({spec.step(sid).name})"


def check_satisfiable(spec: Spec) -> None:
    """判断全部约束是否可满足；不可满足抛 UnsatisfiableError（带冲突链）。"""
    chains: list[str] = []

    # 1) 循环依赖
    cycle = find_cycle(spec)
    if cycle:
        chain = " -> ".join(_name(spec, x) for x in cycle)
        chains.append(f"循环依赖链：{chain}，链上每个步骤都要求排在下一个之前，无解。")
        raise UnsatisfiableError("检测到循环依赖", chains)

    n = len(spec.steps)
    fixed = spec.fixed

    # 2) 固定槽位冲突：两个步骤抢同一个槽位
    slot_owners: dict[int, list[str]] = {}
    for sid, pos in fixed.items():
        slot_owners.setdefault(pos, []).append(sid)
    for pos in sorted(slot_owners):
        owners = slot_owners[pos]
        if len(owners) > 1:
            chains.append(
                f"固定位置冲突：槽位 {pos} 同时被 "
                f"{'、'.join(_name(spec, x) for x in owners)} 占用。"
            )

    # 3) 固定步骤之间的依赖方向与槽位顺序矛盾
    fixed_ids = set(fixed)
    succ, _ = build_graph(spec)

    def reachable(src: str, dst: str) -> list[str] | None:
        """BFS 求 src -> dst 的一条具体路径。"""
        prev: dict[str, str] = {src: src}
        q = deque([src])
        while q:
            u = q.popleft()
            if u == dst:
                path = [dst]
                while path[-1] != src:
                    path.append(prev[path[-1]])
                return path[::-1]
            for v in sorted(succ[u]):
                if v not in prev:
                    prev[v] = u
                    q.append(v)
        return None

    for a in sorted(fixed_ids):
        for b in sorted(fixed_ids):
            if a != b and fixed[a] >= fixed[b]:
                path = reachable(a, b)
                if path:
                    chain = " -> ".join(_name(spec, x) for x in path)
                    chains.append(
                        f"固定位置冲突：{_name(spec, a)} 固定在槽位 {fixed[a]}，"
                        f"{_name(spec, b)} 固定在槽位 {fixed[b]}（更靠前），"
                        f"但依赖要求 {_name(spec, a)} 先于 {_name(spec, b)}：{chain}。"
                    )

    # 4) 固定步骤的前置/后继数量超过槽位可容纳范围
    pred_count: dict[str, int] = {}
    succ_count: dict[str, int] = {}
    for sid in spec.step_ids:
        preds = {x for x in spec.step_ids if reachable(x, sid) is not None and x != sid}
        succs = {x for x in spec.step_ids if reachable(sid, x) is not None and x != sid}
        pred_count[sid] = len(preds)
        succ_count[sid] = len(succs)
    for sid in sorted(fixed):
        pos = fixed[sid]
        if pred_count[sid] > pos:
            chains.append(
                f"固定位置冲突：{_name(spec, sid)} 固定在槽位 {pos}，"
                f"但它有 {pred_count[sid]} 个（传递）前置步骤，槽位之前只有 {pos} 个位置。"
            )
        if succ_count[sid] > n - 1 - pos:
            chains.append(
                f"固定位置冲突：{_name(spec, sid)} 固定在槽位 {pos}，"
                f"但它有 {succ_count[sid]} 个（传递）后继步骤，槽位之后只有 "
                f"{n - 1 - pos} 个位置。"
            )

    if chains:
        raise UnsatisfiableError("固定锚点约束无法同时满足", chains)


# --------------------------------------------------------------------------- #
# 目标序：确定性拓扑排序
# --------------------------------------------------------------------------- #
def build_target_order(spec: Spec) -> list[str]:
    """生成满足全部约束的确定性目标顺序（固定槽位门控的 Kahn 拓扑排序）。

    每轮在入度为 0 的候选中：
    1. 固定步骤只有在“其槽位 == 当前槽位”时才允许放置（门控），保证锚点
       既不被提前也不被推迟；
    2. 可放置的候选里，按（固定槽位优先, 原始下标）排序取最小者——固定步骤
       槽位到达时优先落位，其余按原始下标保证确定性；
    3. 若没有任何可放置候选，说明固定门控与依赖矛盾（check_satisfiable
       理论上已拦截），抛出带现场的 UnsatisfiableError。
    """
    check_satisfiable(spec)
    succ, indeg = build_graph(spec)
    original = {sid: i for i, sid in enumerate(spec.step_ids)}
    n_steps = len(spec.steps)
    placed: set[str] = set()
    target: list[str] = []

    while len(target) < n_steps:
        slot = len(target)
        ready = [sid for sid in spec.step_ids if indeg[sid] == 0 and sid not in placed]
        placeable = [sid for sid in ready
                     if sid not in spec.fixed or spec.fixed[sid] == slot]
        if not placeable:
            blocked = [sid for sid in ready if sid in spec.fixed]
            detail = "、".join(
                f"{_name(spec, sid)} 固定槽位 {spec.fixed[sid]}，当前槽位 {slot}"
                for sid in blocked
            ) or "依赖未释放"
            raise UnsatisfiableError(
                "目标序构造死锁：固定锚点门控与依赖方向冲突",
                [f"槽位 {slot} 无可放置步骤；{detail}。"],
            )
        chosen = sorted(
            placeable,
            key=lambda x: (spec.fixed.get(x, n_steps), original[x]),
        )[0]
        target.append(chosen)
        placed.add(chosen)
        for v in succ[chosen]:
            indeg[v] -= 1

    # 构造后独立复核固定位置
    for sid, pos in spec.fixed.items():
        if target[pos] != sid:
            raise UnsatisfiableError(
                "目标序构造结果与固定锚点不符",
                [f"{_name(spec, sid)} 应在槽位 {pos}，实际落在槽位 "
                 f"{target.index(sid)}。"],
            )
    return target


# --------------------------------------------------------------------------- #
# 最少移动计划：加权 LCS + 反向插入
# --------------------------------------------------------------------------- #
def weighted_lcs(current: list[str], target: list[str],
                 weight: dict[str, int]) -> list[str]:
    """求加权 LCS 对应的公共子序列（固定步骤权重更高，优先保留锚点）。"""
    n, m = len(current), len(target)
    pos_in_target = {sid: i for i, sid in enumerate(target)}
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        ci = current[i - 1]
        for j in range(1, m + 1):
            if ci == target[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + weight.get(ci, 1)
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    keep: list[str] = []
    i, j = n, m
    while i > 0 and j > 0:
        if current[i - 1] == target[j - 1]:
            keep.append(current[i - 1])
            i -= 1
            j -= 1
        elif dp[i - 1][j] >= dp[i][j - 1]:
            i -= 1
        else:
            j -= 1
    keep.reverse()
    # 防御性校验：keep 必须同时是 current 与 target 的子序列
    def is_subseq(seq: list[str], base: list[str]) -> bool:
        it = iter(base)
        return all(x in it for x in seq)
    assert is_subseq(keep, current) and is_subseq(keep, target)
    return keep


def plan_moves(spec: Spec, current: list[str]) -> Plan:
    """基于当前真实顺序 current 生成确定性最少移动计划。

    每次调用都以传入的 current 为准重新计算——外部页面顺序被用户改变后，
    调用方必须重新采集 current 并调用本函数，绝不复用旧下标。
    """
    if sorted(current) != sorted(spec.step_ids):
        raise ValueError("current 与 spec 的步骤集合不一致，无法规划")
    target = build_target_order(spec)
    if current == target:
        return Plan(target=target, moves=[], lcs_size=len(target),
                    lower_bound=0, keep_fixed_untouched=True)

    weight = {sid: (100 if sid in spec.fixed else 1) for sid in spec.step_ids}
    keep = weighted_lcs(current, target, weight)
    keep_set = set(keep)
    moved = [sid for sid in target if sid not in keep_set]

    # 最少移动构造（移动次数恒为 n-LCS，达到理论下界）：
    # 被移动步骤按 target 下标从大到小处理；每次锚点取 target 中该步骤
    # “最近的 LCS 保留元素前驱”，没有则拖到首位。保留元素在整个过程中
    # 互不换位，因此锚点位置稳定，逐次插入后 moved 元素自然落到目标槽位。
    sim = list(current)
    moves: list[Move] = []
    t_index = {sid: i for i, sid in enumerate(target)}
    for sid in sorted(moved, key=lambda x: t_index[x], reverse=True):
        anchor: str | None = None
        for k in range(t_index[sid] - 1, -1, -1):
            if target[k] in keep_set:
                anchor = target[k]
                break
        moves.append(Move(
            step_id=sid,
            anchor_id=anchor,
            note=("拖到首位" if anchor is None else f"拖到 {anchor} 之后"),
        ))
        sim.remove(sid)
        if anchor is None:
            sim.insert(0, sid)
        else:
            sim.insert(sim.index(anchor) + 1, sid)

    if sim != target:
        raise RuntimeError(f"内部错误：模拟重排结果 {sim} != 目标 {target}")

    fixed_touched = sorted(sid for sid in moved if sid in spec.fixed)
    diagnostics = [
        f"目标顺序已确定，加权 LCS 保留 {len(keep)} 个步骤不动："
        f"{'、'.join(keep) if keep else '无'}。",
        f"理论最少移动下界 n-LCS = {len(current)}-{len(keep)} = "
        f"{len(current) - len(keep)}；本计划移动 {len(moves)} 次。",
    ]
    if fixed_touched:
        diagnostics.append(
            "注意：以下固定锚点在当前页面上不在自己的槽位，计划将其拖回："
            + "、".join(f"{sid}->槽位{spec.fixed[sid]}" for sid in fixed_touched)
        )
    return Plan(
        target=target,
        moves=moves,
        lcs_size=len(keep),
        lower_bound=len(current) - len(keep),
        keep_fixed_untouched=not fixed_touched,
        diagnostics=diagnostics,
    )
