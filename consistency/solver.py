"""一致化求解器。

数学形式
--------
内部节点计数由叶子唯一确定（父 = 子之和），根总数 T 固定。因此决策变量只有
叶子计数 y_1..y_L（结构性零叶子恒为 0），要求 y_j >= 0 且总和为 T。目标为

    minimize  sum_v w_v * (x_v - noisy_v)^2

其中 x_v 是节点 v 子树内的叶子计数之和。每个节点对应一个子树叶子集合，子树
集合构成层状族（laminar family），该目标是 M-凸（离散凸）二次函数。

求解方法
--------
把一单位计数加到叶子 j 的边际代价为

    m_j(y) = F(y+e_j) - F(y)
           = sum_{v 为 j 的祖先（含自身、含根）}
             w_v * (2*(x_v - noisy_v) + 1)

M-凸函数的边际代价随自身分配量单调不减；反复把下一单位分给当前边际代价最小
的叶子即得到全局最优整数解。用最小堆维护各叶子当前边际，堆中过期条目在弹出
时按重算值惰性替换，总堆操作数 O((T+L) log L)，可支撑 T=10^6、数百叶子。

平局规则
--------
两套最优解代价相同时，取按叶编号（DFS 先序）排列字典序更小的计数向量。
贪心在边际代价持平时优先把单位分给编号更大的叶子（堆键用负编号），等价于
把计数尽量"压后"分配，从而得到字典序最小的最优叶子向量；该性质由穷举测试
逐例核对。
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Iterable
from dataclasses import dataclass

from .tree import Node, RegionTree


class ConsistencyError(ValueError):
    """输入树无法完成一致化时抛出。"""


@dataclass(frozen=True)
class Reconciliation:
    """校正结果。

    属性：
        counts:            node_id -> 校正后的非负整数计数（父=子之和，根=固定总数）
        total_cost:        按权重计算的平方改动总和 sum_v w_v*(x_v-noisy_v)^2
        leaf_order:        叶编号顺序（DFS 先序）下的 node_id 列表
        structural_zeros:  结构性零叶子的 node_id 集合
    """

    counts: dict[str, int]
    total_cost: int
    leaf_order: list[str]
    structural_zeros: frozenset[str]


def _check_feasibility(tree: RegionTree) -> int:
    total = tree.root.noisy
    if isinstance(total, bool) or not isinstance(total, int):
        raise TypeError("根节点的 noisy（固定公开总数）必须是整数")
    if total < 0:
        raise ConsistencyError(
            f"根的固定公开总数为 {total}，无法分配为非负叶子计数"
        )
    adjustable = [leaf for leaf in tree.leaves if not leaf.structural_zero]
    if total > 0 and not adjustable:
        raise ConsistencyError(
            "所有叶子均为结构性零，但根的固定公开总数大于 0，问题不可行"
        )
    return total


def _index_nodes(tree: RegionTree) -> tuple[list[Node], dict[str, int]]:
    # nodes 字典的插入顺序即构建时的 DFS 先序
    node_list = list(tree.nodes.values())
    index = {node.node_id: i for i, node in enumerate(node_list)}
    return node_list, index


def _leaf_chains(
    tree: RegionTree, index: dict[str, int]
) -> list[list[int]]:
    """返回可调节叶子的祖先节点下标链（按叶编号 DFS 先序）。"""
    chains: list[list[int]] = []
    for leaf in tree.leaves:
        if leaf.structural_zero:
            continue
        chain = [index[node.node_id] for node in tree.ancestors_of_leaf(leaf)]
        chains.append(chain)
    return chains


def reconcile(tree: RegionTree) -> Reconciliation:
    """对地区树执行一致化，返回每个节点的校正计数与总代价。

    参数：
        tree: 由 :meth:`RegionTree.build` 校验过的地区树；根的 noisy 即固定
              公开总数（非负整数）。

    抛出：
        ConsistencyError: 根总数为负，或全部叶子为结构性零而总数大于 0。
    """

    if not isinstance(tree, RegionTree):
        raise TypeError("tree 必须是 RegionTree 实例")

    total = _check_feasibility(tree)
    node_list, index = _index_nodes(tree)
    n = len(node_list)
    chains = _leaf_chains(tree, index)
    leaf_count = len(chains)

    noisy = [node.noisy for node in node_list]
    weight = [node.weight for node in node_list]
    counts = [0] * n

    # 边际 m_j = sum_{v in chain_j} w_v*(2*(x_v-noisy_v)+1)
    #         = 2 * sum_{v in chain_j} w_v*x_v + const_j
    # 预算 const_j 后，热循环里每次边际计算只剩一次加权和。
    chain_const = [
        sum(weight[v] * (1 - 2 * noisy[v]) for v in chain) for chain in chains
    ]
    chain_weights = [[weight[v] for v in chain] for chain in chains]

    def marginal(j: int) -> int:
        chain = chains[j]
        cw = chain_weights[j]
        acc = chain_const[j]
        for idx, v in enumerate(chain):
            acc += 2 * cw[idx] * counts[v]
        return acc

    # 堆条目：(当前边际, -叶子序号)；边际持平时序号更大的先弹出，
    # 得到按叶编号字典序最小的最优计数向量。
    # 其他叶子分配会让本条目过期，弹出堆顶时按当前计数重算并惰性替换。
    heap: list[tuple[int, int]] = [
        (marginal(j), -j) for j in range(leaf_count)
    ]
    heapq.heapify(heap)
    heappop = heapq.heappop
    heapreplace = heapq.heapreplace
    heappush = heapq.heappush

    for _ in range(total):
        while True:
            entry_m, neg_j = heap[0]
            j = -neg_j
            current_m = marginal(j)
            if current_m == entry_m:
                break
            heapreplace(heap, (current_m, neg_j))

        heappop(heap)
        for v in chains[j]:
            counts[v] += 1
        heappush(heap, (marginal(j), neg_j))

    result_counts = {
        node_list[v].node_id: counts[v] for v in range(n)
    }
    total_cost = sum(
        weight[v] * (counts[v] - noisy[v]) ** 2 for v in range(n)
    )

    return Reconciliation(
        counts=result_counts,
        total_cost=total_cost,
        leaf_order=[leaf.node_id for leaf in tree.leaves],
        structural_zeros=frozenset(
            leaf.node_id for leaf in tree.leaves if leaf.structural_zero
        ),
    )


# ---------------------------------------------------------------------------
# 穷举核对（仅用于小树）
# ---------------------------------------------------------------------------


def _compositions(total: int, parts: int):
    """枚举长度为 parts、非负整数分量之和为 total 的所有组合（字典序）。"""
    if parts == 1:
        yield (total,)
        return
    for head in range(total + 1):
        for tail in _compositions(total - head, parts - 1):
            yield (head,) + tail


def brute_force_optimal(
    tree: RegionTree, *, max_combinations: int = 200_000
) -> Reconciliation:
    """穷举所有可行叶子计数向量，返回最优解（小树核对用）。

    与 :func:`reconcile` 使用完全相同的平局规则：代价相同时取按叶编号
    字典序最小的向量。枚举组合数超过 max_combinations 时抛出 ValueError。
    """

    if not isinstance(tree, RegionTree):
        raise TypeError("tree 必须是 RegionTree 实例")

    total = _check_feasibility(tree)
    adjustable = [leaf for leaf in tree.leaves if not leaf.structural_zero]
    parts = len(adjustable)
    combinations_count = (
        1 if parts == 0 else math.comb(total + parts - 1, parts - 1)
    )
    if combinations_count > max_combinations:
        raise ValueError(
            f"穷举组合数 {combinations_count} 超过上限 {max_combinations}，"
            "请改用 reconcile() 或调小问题规模"
        )

    node_list, index = _index_nodes(tree)
    n = len(node_list)
    noisy = [node.noisy for node in node_list]
    weight = [node.weight for node in node_list]
    depth = [tree.depth[node.node_id] for node in node_list]
    child_idx = [
        [index[child.node_id] for child in node.children] for node in node_list
    ]
    bottom_up = sorted(range(n), key=lambda v: depth[v], reverse=True)

    adjustable_pos = {leaf.node_id: j for j, leaf in enumerate(adjustable)}
    zero_leaves = {
        leaf.node_id for leaf in tree.leaves if leaf.structural_zero
    }

    best_cost: int | None = None
    best_vector: tuple[int, ...] | None = None
    best_counts: list[int] | None = None

    if parts == 0:
        # 无可调节叶子：可行性已由 _check_feasibility 保证（此时 total == 0）
        vectors: Iterable[tuple[int, ...]] = [()]
    else:
        vectors = _compositions(total, parts)

    for vector in vectors:
        leaf_counts = [0] * n
        for leaf in tree.leaves:
            if leaf.node_id in zero_leaves:
                continue
            leaf_counts[index[leaf.node_id]] = vector[
                adjustable_pos[leaf.node_id]
            ]

        x = leaf_counts[:]
        for v in bottom_up:
            if child_idx[v]:
                x[v] = sum(x[c] for c in child_idx[v])

        cost = sum(weight[v] * (x[v] - noisy[v]) ** 2 for v in range(n))
        if best_cost is None or cost < best_cost or (
            cost == best_cost and best_vector is not None and vector < best_vector
        ):
            best_cost = cost
            best_vector = vector
            best_counts = x

    assert best_counts is not None and best_cost is not None
    return Reconciliation(
        counts={node_list[v].node_id: best_counts[v] for v in range(n)},
        total_cost=best_cost,
        leaf_order=[leaf.node_id for leaf in tree.leaves],
        structural_zeros=frozenset(zero_leaves),
    )
