"""信封接收节点规划的核心实现。

问题模型
========
一棵分发树（LKH 风格）：

- 每个节点对应一把彼此独立的节点钥；
- 叶子对应成员，成员持有根到自身叶子整条路径上的全部节点钥；
- 在节点 v 放置一个信封，凡是位于 v 子树内、且实际持有节点钥的叶子都能打开它。

输入
----
- ``members``：本期在册成员叶子（必须全部被覆盖）；
- ``holders``：实际持钥叶子集合（离组但仍持钥者也可能出现在这里）；
- ``leaked``：已泄露节点钥对应的节点（这些节点不能再放信封）；
- ``costs``：每个可用节点的封装成本（未给出成本的节点视为不可用）；
- ``leavers``：显式离组者；缺省取 ``holders - members``。

安全约束
--------
节点 v 可以放信封，当且仅当：

1. v 在 ``costs`` 中（可用）；
2. v 不在 ``leaked`` 中（节点钥未泄露，仅影响 v 自身，不连累祖先/后代）；
3. v 的可打开集合（v 子树内的持钥叶子）中不含任何离组者。

目标
----
覆盖每个在册成员叶子，最小化总成本；平局取排序后节点编号序列字典序最小。
不可行时返回每一个无法覆盖的叶子及其具体原因。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite as math_isfinite
from numbers import Real
from typing import Any, Iterable, Mapping, Optional


class InputError(ValueError):
    """输入结构不合法（区别于“输入合法但无可行方案”）。"""


@dataclass(frozen=True)
class Envelope:
    """一个可模拟的封装信封。"""

    node: Any  # 放置信封的节点编号
    cost: Any  # 该节点的封装成本
    recipients: tuple  # 能够打开该信封的持钥叶子（已排序）

    def to_dict(self) -> dict:
        return {
            "node": self.node,
            "cost": self.cost,
            "recipients": list(self.recipients),
        }


@dataclass(frozen=True)
class UncoveredLeaf:
    """不可行方案中无法覆盖的在册叶子及原因。"""

    leaf: Any
    reason: str  # missing_leaf_key | no_safe_envelope
    detail: str

    def to_dict(self) -> dict:
        return {"leaf": self.leaf, "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class EnvelopePlan:
    """规划结果。``feasible=False`` 时信封列表为空，并给出 uncovered 诊断。"""

    feasible: bool
    envelopes: tuple[Envelope, ...] = ()
    total_cost: Any = None
    covered_leaves: tuple = ()
    uncovered_leaves: tuple[UncoveredLeaf, ...] = ()

    @property
    def node_sequence(self) -> tuple:
        """排序后的信封节点编号序列（平局比较与结果模拟使用）。"""
        return tuple(e.node for e in self.envelopes)

    def to_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "total_cost": self.total_cost,
            "envelopes": [e.to_dict() for e in self.envelopes],
            "covered_leaves": list(self.covered_leaves),
            "uncovered_leaves": [u.to_dict() for u in self.uncovered_leaves],
        }


class DistributionTree:
    """不可变分发树，通过 ``{节点: 父节点}`` 映射构造，根的父节点为 ``None``。"""

    def __init__(self, parent: Mapping[Any, Optional[Any]]):
        if not isinstance(parent, Mapping):
            raise InputError("parent 必须是 {节点: 父节点} 映射，根的父节点为 None")
        if len(parent) == 0:
            raise InputError("分发树不能为空")

        self._parent: dict[Any, Optional[Any]] = dict(parent)
        self._children: dict[Any, list[Any]] = {v: [] for v in self._parent}
        roots = []
        for node, par in self._parent.items():
            if par is None:
                roots.append(node)
                continue
            if par not in self._parent:
                raise InputError(f"节点 {node!r} 的父节点 {par!r} 不在树中")
            self._children[par].append(node)
        if len(roots) != 1:
            raise InputError(f"分发树必须恰好有一个根，实际找到 {len(roots)} 个：{roots!r}")
        self._root = roots[0]

        # 迭代式后序遍历，同时检测环/不可达节点。
        self._postorder: list[Any] = []
        seen: set[Any] = set()
        stack: list[tuple[Any, int]] = [(self._root, 0)]
        while stack:
            node, state = stack.pop()
            if state == 0:
                if node in seen:
                    raise InputError(f"检测到环或重复访问，节点 {node!r}")
                seen.add(node)
                stack.append((node, 1))
                for child in self._children[node]:
                    stack.append((child, 0))
            else:
                self._postorder.append(node)
        if len(seen) != len(self._parent):
            orphan = [v for v in self._parent if v not in seen]
            raise InputError(f"存在与根不连通的节点：{orphan!r}")

        self._leaves = frozenset(
            v for v in self._parent if not self._children[v]
        )
        # 每个节点子树内的全部叶子。
        self._subtree_leaves: dict[Any, frozenset[Any]] = {}
        for v in self._postorder:
            if v in self._leaves:
                self._subtree_leaves[v] = frozenset({v})
            else:
                merged: set[Any] = set()
                for c in self._children[v]:
                    merged.update(self._subtree_leaves[c])
                self._subtree_leaves[v] = frozenset(merged)

    @property
    def root(self) -> Any:
        return self._root

    @property
    def nodes(self) -> frozenset:
        return frozenset(self._parent)

    @property
    def leaves(self) -> frozenset:
        return self._leaves

    def parent_of(self, node: Any) -> Optional[Any]:
        if node not in self._parent:
            raise InputError(f"未知节点 {node!r}")
        return self._parent[node]

    def children_of(self, node: Any) -> tuple:
        if node not in self._parent:
            raise InputError(f"未知节点 {node!r}")
        return tuple(self._children[node])

    def path_to_root(self, leaf: Any) -> tuple:
        """返回叶子到根路径上的全部节点（含叶子自身与根），自底向上。"""
        if leaf not in self._parent:
            raise InputError(f"未知节点 {leaf!r}")
        path = []
        cur: Optional[Any] = leaf
        while cur is not None:
            path.append(cur)
            cur = self._parent[cur]
        return tuple(path)

    def subtree_leaves(self, node: Any) -> frozenset:
        if node not in self._parent:
            raise InputError(f"未知节点 {node!r}")
        return self._subtree_leaves[node]

    @property
    def postorder(self) -> tuple:
        return tuple(self._postorder)


def _as_id_set(values: Iterable[Any], name: str) -> set:
    try:
        result = set(values)
    except TypeError as exc:
        raise InputError(f"{name} 中的节点编号必须可哈希") from exc
    if any(v is None for v in result):
        raise InputError(f"{name} 不能包含 None")
    return result


def _check_leaf_ids(values: set, tree: DistributionTree, name: str) -> None:
    unknown = values - tree.leaves
    if unknown:
        raise InputError(f"{name} 包含非叶子或未知编号：{sorted(unknown, key=repr)!r}")


def plan_envelopes(
    tree: DistributionTree,
    members: Iterable[Any],
    holders: Iterable[Any],
    leaked: Iterable[Any],
    costs: Mapping[Any, Any],
    leavers: Optional[Iterable[Any]] = None,
) -> EnvelopePlan:
    """为下一期群组密钥规划信封接收节点。

    返回 :class:`EnvelopePlan`；输入结构错误抛 :class:`InputError`，
    输入合法但无法覆盖全部在册叶子时返回 ``feasible=False`` 的方案并逐叶诊断。
    """
    if not isinstance(tree, DistributionTree):
        raise InputError("tree 必须是 DistributionTree 实例")
    if not isinstance(costs, Mapping):
        raise InputError("costs 必须是 {节点: 封装成本} 映射")

    member_set = _as_id_set(members, "members")
    holder_set = _as_id_set(holders, "holders")
    leaked_set = _as_id_set(leaked, "leaked")
    _check_leaf_ids(member_set, tree, "members")
    _check_leaf_ids(holder_set, tree, "holders")

    unknown_leaked = leaked_set - tree.nodes
    if unknown_leaked:
        raise InputError(f"leaked 包含未知节点编号：{sorted(unknown_leaked, key=repr)!r}")

    cost_map: dict[Any, Any] = {}
    for node, cost in costs.items():
        if node not in tree.nodes:
            raise InputError(f"costs 包含未知节点编号：{node!r}")
        if isinstance(cost, bool) or not isinstance(cost, Real):
            raise InputError(f"节点 {node!r} 的成本必须是数值，实际为 {cost!r}")
        if cost < 0:
            raise InputError(f"节点 {node!r} 的封装成本不能为负：{cost!r}")
        if isinstance(cost, float) and not math_isfinite(cost):
            raise InputError(f"节点 {node!r} 的封装成本必须有限：{cost!r}")
        cost_map[node] = cost
    available = set(cost_map)

    if leavers is None:
        leaver_set = holder_set - member_set
    else:
        leaver_set = _as_id_set(leavers, "leavers")
        _check_leaf_ids(leaver_set, tree, "leavers")
        overlap = leaver_set & member_set
        if overlap:
            raise InputError(f"离组者与在册成员不能重叠：{sorted(overlap, key=repr)!r}")

    # 节点是否会被离组者打开（离组者位于其子树内即命中，持钥性由 leaver 集合本身保证）。
    leaver_blocked: set[Any] = set()
    for v in tree.postorder:
        if tree.subtree_leaves(v) & leaver_set:
            leaver_blocked.add(v)

    def eligible(v: Any) -> bool:
        return (
            v in available
            and v not in leaked_set
            and v not in leaver_blocked
        )

    def block_detail(v: Any) -> str:
        reasons = []
        if v in leaked_set:
            reasons.append("节点钥已泄露")
        if v not in available:
            reasons.append("未提供封装成本（节点不可用）")
        if v in leaver_blocked:
            who = sorted(tree.subtree_leaves(v) & leaver_set, key=repr)
            reasons.append(f"可被离组者 {who!r} 打开")
        return f"节点 {v!r}：" + "、".join(reasons)

    # ---- 可行性预检：逐叶判断是否存在安全可打开的信封位置 ----
    uncovered: list[UncoveredLeaf] = []
    for leaf in sorted(member_set, key=repr):
        if leaf not in holder_set:
            uncovered.append(
                UncoveredLeaf(
                    leaf=leaf,
                    reason="missing_leaf_key",
                    detail="该在册叶子不在实际持钥集合中，路径上没有它能打开的节点钥",
                )
            )
            continue
        path = tree.path_to_root(leaf)
        safe_nodes = [v for v in path if eligible(v)]
        if not safe_nodes:
            detail = "；".join(block_detail(v) for v in path)
            uncovered.append(
                UncoveredLeaf(
                    leaf=leaf,
                    reason="no_safe_envelope",
                    detail="根到叶子路径上的节点全部不可选：" + detail,
                )
            )

    if uncovered:
        return EnvelopePlan(
            feasible=False,
            envelopes=(),
            total_cost=None,
            covered_leaves=(),
            uncovered_leaves=tuple(uncovered),
        )

    # ---- 树形 DP：子树 v 覆盖其中全部在册成员的最优（成本, 节点序列, 覆盖集）----
    # 方案空间规范为反链覆盖：选中节点互不为祖先，即不含冗余信封。
    # 成本非负时，任何含冗余信封的方案删除冗余后成本不增、覆盖不变，
    # 因此最优解一定可在反链方案中取得（零成本边界下同样取最精简方案）。
    # 状态 None 表示该子树不可行（预检通过后根状态必可行）。
    dp: dict[Any, Optional[tuple[Any, tuple, frozenset]]] = {}
    for v in tree.postorder:
        member_leaves = tree.subtree_leaves(v) & member_set
        if not member_leaves:
            dp[v] = (0, (), frozenset())
            continue

        # 方案 A：直接在 v 放一个信封，覆盖子树内全部在册成员。
        option_a: Optional[tuple[Any, tuple, frozenset]] = None
        if eligible(v):
            option_a = (cost_map[v], (v,), frozenset(member_leaves))

        # 方案 B：不在 v 放信封，合并各孩子子树的最优方案。
        # 注意：无成员孩子的空状态不计入覆盖，必须显式校验覆盖完整性，
        # 否则叶子节点会被成本 0 的空方案冒充。
        option_b: Optional[tuple[Any, tuple, frozenset]] = None
        child_states = [dp[c] for c in tree.children_of(v)]
        if all(state is not None for state in child_states):
            total = 0
            picked: list[Any] = []
            covered: set[Any] = set()
            for cost, seq, cov in child_states:
                total += cost
                picked.extend(seq)
                covered.update(cov)
            if covered == member_leaves:
                option_b = (total, tuple(sorted(picked)), frozenset(covered))

        candidates = [opt for opt in (option_a, option_b) if opt is not None]
        if not candidates:
            # 自身不可放信封，又无法由孩子组合覆盖（叶子被封死时即命中）。
            dp[v] = None
        else:
            # 先比总成本，再比排序后节点编号序列的字典序（Python 元组比较：
            # 短序列若是长序列前缀则短者更小，等价于信封数更少者优先）。
            dp[v] = min(candidates, key=lambda state: (state[0], state[1]))

    _, chosen_seq, _ = dp[tree.root]  # type: ignore[misc]

    envelopes: list[Envelope] = []
    total_cost = 0
    for v in sorted(chosen_seq, key=repr):
        recipients = tuple(sorted(tree.subtree_leaves(v) & holder_set, key=repr))
        envelopes.append(Envelope(node=v, cost=cost_map[v], recipients=recipients))
        total_cost += cost_map[v]

    return EnvelopePlan(
        feasible=True,
        envelopes=tuple(envelopes),
        total_cost=total_cost,
        covered_leaves=tuple(sorted(member_set, key=repr)),
        uncovered_leaves=(),
    )


def simulate_delivery(
    plan: EnvelopePlan,
    tree: DistributionTree,
    holders: Iterable[Any],
) -> dict[Any, list[Any]]:
    """模拟信封投递：返回 ``{信封节点: 能打开它的持钥叶子}``。

    能打开的条件：叶子位于信封节点子树内且在实际持钥集合中。
    调用方可据此断言在册成员均能打开、离组者一个都打不开。
    """
    if not isinstance(plan, EnvelopePlan):
        raise InputError("plan 必须是 EnvelopePlan 实例")
    holder_set = _as_id_set(holders, "holders")
    _check_leaf_ids(holder_set, tree, "holders")

    openers: dict[Any, list[Any]] = {}
    for env in plan.envelopes:
        who = sorted(tree.subtree_leaves(env.node) & holder_set, key=repr)
        openers[env.node] = who
    return openers
