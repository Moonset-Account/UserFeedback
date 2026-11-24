"""按票据查询的极小化最坏轮数博弈。

查询规则（来自现场约束）：

- 节点只支持按搬迁票据查询：一次询问 (ticket, node) 返回该票据在该节点
  上是否真的落位（布尔）；
- 一轮至多询问两个节点（可以是两张票据各一个节点，也可以同票据两节点）；
- 只有当前所有可能现场都安全时，才允许继续撤机。

本模块用极小极大 DP 求最少的最坏查询轮数，并给出两份再少一轮就会误放行的
不可区分现场作为下界证明。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from itertools import combinations

from .logs import MigrationTicket, World, possible_worlds, shared_log_prefix


@dataclass(frozen=True)
class SafetyContext:
    """判定一个可能现场是否安全的上下文。

    safe_worlds 直接给出安全现场的票据落位键；unsafe 现场即其补集。
    调用方通常依据“该落位是否会让某节点超配 / 违反异机或区域差”构造。
    """

    safe_worlds: frozenset[tuple[str, ...]]

    def is_safe(self, world: World, tickets: tuple[MigrationTicket, ...]) -> bool:
        key = tuple(world.placement[t.ticket] for t in tickets)
        return key in self.safe_worlds


@dataclass(frozen=True)
class StrategyNode:
    """决策树的一层：本轮询问的 (票据, 节点) 对与各回答分支。"""

    queries: tuple[tuple[str, str], ...]
    branches: dict[int, "StrategyNode | bool"] = field(default_factory=dict)

    @property
    def is_leaf(self) -> bool:
        return not self.queries


@dataclass(frozen=True)
class UncertaintySolution:
    """查询博弈的解。

    - rounds: 最少的最坏查询轮数；
    - strategy: 可执行的自适应决策树根；
    - witness: 两份现场（一不安全一安全），它们沿同一条回答路径共处
      rounds-1 轮而不被分开——这就是“再少一轮就会误放行”的证据；
    - witness_query_path: 这 rounds-1 轮实际发出的询问；
    - witness_answers: 两份现场在每一轮上给出的完全相同的回答位；
    - witness_distance: 两份现场在票据落位上的 Hamming 距离（仅辅助信息，
      真正的不可区分判据是回答序列相同，而非距离大小）；
    - shared_prefix: 两份现场共用的日志前缀是否确实一致。
    """

    rounds: int
    strategy: StrategyNode
    witness: tuple[World, World] | None
    witness_query_path: tuple[tuple[tuple[str, str], ...], ...]
    witness_answers: tuple[int, ...]
    witness_distance: int
    shared_prefix: bool
    all_safe_initially: bool


# ---------------------------------------------------------------------------
# 工具：把一轮询问编码成位掩码，对现场求回答
# ---------------------------------------------------------------------------

def _query_universe(tickets: tuple[MigrationTicket, ...]) -> tuple[tuple[str, str], ...]:
    universe: list[tuple[str, str]] = []
    for ticket in tickets:
        for node in ticket.candidates:
            universe.append((ticket.ticket, node))
    return tuple(universe)


def _answer_bits(world: World, queries: tuple[tuple[str, str], ...]) -> int:
    bits = 0
    for i, (ticket, node) in enumerate(queries):
        if world.placement[ticket] == node:
            bits |= 1 << i
    return bits


def _world_key(world: World, tickets: tuple[MigrationTicket, ...]) -> tuple[str, ...]:
    return tuple(world.placement[t.ticket] for t in tickets)


# ---------------------------------------------------------------------------
# 极小极大 DP
# ---------------------------------------------------------------------------

def solve_query_game(tickets: list[MigrationTicket],
                     context: SafetyContext) -> UncertaintySolution:
    """计算最少最坏查询轮数、决策树与误放行下界证明。"""
    ticket_tuple = tuple(tickets)
    worlds = possible_worlds(ticket_tuple)
    universe = _query_universe(ticket_tuple)
    safe_keys = {
        _world_key(w, ticket_tuple)
        for w in worlds
        if context.is_safe(w, ticket_tuple)
    }
    initial_keys = frozenset(_world_key(w, ticket_tuple) for w in worlds)
    all_safe = initial_keys <= safe_keys

    # 一轮可选 1 或 2 个询问点。
    one_point = [(q,) for q in universe]
    two_points = [tuple(pair) for pair in combinations(universe, 2)]
    rounds_choices = one_point + two_points

    @lru_cache(maxsize=None)
    def solve(alive_keys: frozenset[tuple[str, ...]]) -> tuple[int, StrategyNode]:
        # 全部安全：无需再问，直接放行。
        if alive_keys <= safe_keys:
            return 0, StrategyNode((), {0: True})
        # 只剩唯一一个（不安全）现场：身份已完全确定，无需再问，
        # 直接判定不可放行。这是“拒绝”叶子，与放行叶子 True 区分。
        if len(alive_keys) == 1:
            return 0, StrategyNode((), {0: False})

        best: tuple[int, StrategyNode] | None = None
        for queries in rounds_choices:
            partition: dict[int, set[tuple[str, ...]]] = {}
            for key in alive_keys:
                world = World({ticket_tuple[i].ticket: key[i]
                               for i in range(len(ticket_tuple))})
                bits = _answer_bits(world, queries)
                partition.setdefault(bits, set()).add(key)

            # 只有当“所有”仍含不安全现场的分支都没有收缩时，
            # 这组询问才毫无进展（例如询问点与任何存活现场都不匹配）。
            unsafe_branches = [
                frozenset(members)
                for members in partition.values()
                if not frozenset(members) <= safe_keys
            ]
            if unsafe_branches and all(
                branch == alive_keys for branch in unsafe_branches
            ):
                continue

            worst = 0
            branches: dict[int, StrategyNode | bool] = {}
            for bits, members in partition.items():
                member_keys = frozenset(members)
                if member_keys <= safe_keys:
                    # 该分支已全部安全：本轮问完即可放行，不再递归。
                    branches[bits] = True
                    worst = max(worst, 1)
                    continue
                sub_rounds, sub_tree = solve(member_keys)
                # 子树若是零成本叶子（唯一不安全现场，直接拒绝），
                # 则成本只算当前这一轮；否则当前轮 + 子树轮数。
                worst = max(worst, 1 + sub_rounds)
                branches[bits] = sub_tree
            candidate = (worst, StrategyNode(queries, branches))
            if best is None or candidate[0] < best[0]:
                best = candidate

        if best is None:
            # 理论不可达：多于一个候选时，询问点空间非空总能区分。
            raise RuntimeError("查询博弈无法收敛")
        return best

    rounds, strategy = solve(initial_keys)

    # 下界证明：沿最优决策树找到一条在 rounds-1 轮后仍同时包含安全与不安全
    # 现场的回答路径。两份现场在这条路径上对每一轮询问给出完全相同的回答，
    # 因此任何少于 rounds 轮的策略都无法把它们分开，必然在不安全现场上误放行。
    key_to_world = {
        _world_key(w, ticket_tuple): w for w in worlds
    }
    witness, query_path, answer_path = _extract_indistinguishable_pair(
        strategy, rounds, safe_keys, key_to_world, ticket_tuple
    )
    if witness is not None:
        bad_world, good_world = witness
        bad_key = _world_key(bad_world, ticket_tuple)
        good_key = _world_key(good_world, ticket_tuple)
        distance = sum(a != b for a, b in zip(bad_key, good_key))
        # 可执行校验：两份现场在路径上的每一轮回答必须逐位相同。
        for queries, bits in zip(query_path, answer_path):
            assert _answer_bits(bad_world, queries) == bits
            assert _answer_bits(good_world, queries) == bits
    else:
        distance = 0

    prefix_ok = shared_log_prefix(ticket_tuple, worlds) if len(worlds) >= 2 else True
    return UncertaintySolution(
        rounds=rounds,
        strategy=strategy,
        witness=witness,
        witness_query_path=tuple(query_path),
        witness_answers=tuple(answer_path),
        witness_distance=distance,
        shared_prefix=prefix_ok,
        all_safe_initially=all_safe,
    )


def _world_from_key(key: tuple[str, ...],
                    tickets: tuple[MigrationTicket, ...]) -> World:
    return World({tickets[i].ticket: key[i] for i in range(len(tickets))})


def _extract_indistinguishable_pair(
    root: StrategyNode,
    rounds: int,
    safe_keys: frozenset[tuple[str, ...]],
    key_to_world: dict[tuple[str, ...], World],
    tickets: tuple[MigrationTicket, ...],
):
    """在最优树的深度 rounds-1 处搜索一个仍混合的节点作为下界证据。

    若该深度存在同时含安全与不安全现场的节点，则其中两份现场对前 rounds-1
    轮的每个询问回答完全相同、必须靠第 rounds 轮才能分开，这就是严格的
    “再少一轮就会误放行”证据。若该深度所有节点都已纯净，说明在这组现场上
    不存在这样的证据对，返回 None（调用方据此只保留轮数结论）。
    """
    if rounds == 0:
        return None, [], []

    # 广度收集 (节点, 该节点存活集合, 到达该节点的询问路径与回答路径)。
    frontier: list[tuple[
        StrategyNode,
        frozenset[tuple[str, ...]],
        list[tuple[tuple[str, str], ...]],
        list[int],
    ]] = [
        (
            root,
            frozenset(key_to_world),
            [],
            [],
        )
    ]
    for depth in range(rounds - 1):
        nxt: list[tuple] = []
        for node, alive, qpath, apath in frontier:
            if isinstance(node, bool) or node.is_leaf:
                continue
            for bits in node.branches:
                members = _branch_members(node, bits, alive, tickets)
                if not members:
                    continue
                nxt.append((
                    node.branches[bits],
                    members,
                    qpath + [node.queries],
                    apath + [bits],
                ))
        frontier = nxt
        if not frontier:
            return None, [], []

    # 在深度 rounds-1 的节点里找混合节点。
    for node, alive, qpath, apath in frontier:
        has_safe = any(k in safe_keys for k in alive)
        has_unsafe = any(k not in safe_keys for k in alive)
        if has_safe and has_unsafe:
            unsafe_key = next(k for k in alive if k not in safe_keys)
            safe_key = next(k for k in alive if k in safe_keys)
            return (
                (key_to_world[unsafe_key], key_to_world[safe_key]),
                qpath,
                apath,
            )
    return None, [], []


def _branch_members(node: StrategyNode, bits: int,
                    alive: frozenset[tuple[str, ...]],
                    tickets: tuple[MigrationTicket, ...]
                    ) -> frozenset[tuple[str, ...]]:
    return frozenset(
        k for k in alive
        if _answer_bits(_world_from_key(k, tickets), node.queries) == bits
    )
