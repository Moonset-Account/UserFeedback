"""信封规划模块的 unittest 验收用例。

覆盖维度：
1. 可接受结果：根节点胜出、叶子胜出、离组者阻断祖先、泄露仅影响单节点、字典序平局；
2. 应被阻止/拒绝：持钥缺失、路径全被安全约束封死、非法输入；
3. 边界/交错：零成本节点、空成员集合、显式离组者、显式离组者不在 holders、
   枚举全部可行子集与 DP 结果交叉校验；
4. 投递模拟：在册成员至少能打开一个信封，离组者一个都打不开。
"""

import itertools
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from envelope_planner import (  # noqa: E402
    DistributionTree,
    EnvelopePlan,
    InputError,
    plan_envelopes,
    simulate_delivery,
)


def build_tree():
    """测试树：

            0
          /   \\
         1     2
        / \\   / \\
       3   4 5   6
    """
    parent = {0: None, 1: 0, 2: 0, 3: 1, 4: 1, 5: 2, 6: 2}
    return DistributionTree(parent)


LEAVES = [3, 4, 5, 6]
ALL_NODES = [0, 1, 2, 3, 4, 5, 6]


def _is_ancestor(tree, anc, node):
    """anc 是否为 node 的祖先（含自身）。"""
    return anc in tree.path_to_root(node)


def brute_force_best(tree, members, holders, leaked, costs, leavers):
    """朴素枚举：遍历可用节点全部子集，返回 (可行?, 最优(成本, 节点序列))。

    与 DP 独立实现，用于交叉验证成本最优性与字典序平局规则。
    """
    member_set = set(members)
    holder_set = set(holders)
    leaver_set = set(leavers) if leavers is not None else (holder_set - member_set)
    leaked_set = set(leaked)

    eligible = []
    for v in tree.nodes:
        if v not in costs or v in leaked_set:
            continue
        if tree.subtree_leaves(v) & leaver_set:
            continue
        eligible.append(v)

    best = None  # (成本, 排序节点序列, 子集集合)
    for r in range(len(eligible) + 1):
        for subset in itertools.combinations(eligible, r):
            # 与规划模块同一方案空间：反链覆盖（任一选中节点都不是另一选中节点
            # 的祖先），即不含冗余信封；非负成本下冗余信封只会让序列字典序变大。
            if any(
                _is_ancestor(tree, v, other) or _is_ancestor(tree, other, v)
                for i, v in enumerate(subset)
                for other in subset[i + 1 :]
            ):
                continue
            covered = set()
            for v in subset:
                covered |= tree.subtree_leaves(v) & holder_set
            if not member_set <= covered:
                continue
            seq = tuple(sorted(subset))
            total = sum(costs[v] for v in subset)
            cand = (total, seq, set(subset))
            # 题面平局规则：排序后节点编号序列字典序最小；
            # 标准字典序下短序列若是长序列前缀则短者更小。
            if best is None or (total, seq) < (best[0], best[1]):
                best = cand
    if best is None:
        return False, None
    return True, (best[0], best[1])


class AcceptablePlanTests(unittest.TestCase):
    def setUp(self):
        self.tree = build_tree()

    def test_root_wins_when_cheapest(self):
        """可接受结果：根信封单点覆盖全员且最便宜。"""
        costs = {v: 10 for v in ALL_NODES}
        costs[0] = 1  # 根最便宜
        plan = plan_envelopes(self.tree, LEAVES, LEAVES, [], costs)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.node_sequence, (0,))
        self.assertEqual(plan.total_cost, 1)
        self.assertEqual(plan.covered_leaves, tuple(LEAVES))
        # 每个信封的接收者即子树内持钥叶子，可直接模拟。
        self.assertEqual(plan.envelopes[0].recipients, tuple(LEAVES))
        self._assert_matches_brute_force(LEAVES, LEAVES, [], costs, None, plan)

    def test_leaf_envelopes_win_when_cheaper(self):
        """可接受结果：内部节点昂贵时，选全部叶子信封。"""
        costs = {v: 100 for v in ALL_NODES}
        for leaf in LEAVES:
            costs[leaf] = 1  # 叶子各 1，合计 4
        plan = plan_envelopes(self.tree, LEAVES, LEAVES, [], costs)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.node_sequence, (3, 4, 5, 6))
        self.assertEqual(plan.total_cost, 4)
        self._assert_matches_brute_force(LEAVES, LEAVES, [], costs, None, plan)

    def test_leaver_blocks_all_ancestor_envelopes(self):
        """离组叶子 6 阻断根 0 与节点 2；其余成员改由节点 1 与叶子 5 覆盖。"""
        members = [3, 4, 5]
        holders = [3, 4, 5, 6]  # 6 离组但仍持钥
        costs = {v: 10 for v in ALL_NODES}
        costs[1] = 1
        plan = plan_envelopes(self.tree, members, holders, [], costs)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.node_sequence, (1, 5))
        self.assertEqual(plan.total_cost, 11)

        # 模拟投递：离组者 6 打不开任何信封。
        openers = simulate_delivery(plan, self.tree, holders)
        can_open = {leaf for leaves in openers.values() for leaf in leaves}
        self.assertEqual(set(can_open), set(members))
        self.assertNotIn(6, can_open)
        # 节点 1 的信封只被 3、4 打开；叶子 5 的信封只被 5 打开。
        self.assertEqual(openers[1], [3, 4])
        self.assertEqual(openers[5], [5])
        self._assert_matches_brute_force(members, holders, [], costs, None, plan)

    def test_explicit_leaver_not_in_holders_still_blocks(self):
        """显式声明的离组者即使不在 holders 中，仍按风险阻断其祖先节点。"""
        members = [3, 4, 5]
        holders = [3, 4, 5]
        costs = {v: 10 for v in ALL_NODES}
        costs[1] = 1
        plan = plan_envelopes(
            self.tree, members, holders, [], costs, leavers=[6]
        )
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.node_sequence, (1, 5))
        openers = simulate_delivery(plan, self.tree, holders)
        flat = {leaf for leaves in openers.values() for leaf in leaves}
        self.assertEqual(flat, set(members))
        self._assert_matches_brute_force(members, holders, [], costs, [6], plan)

    def test_leaked_node_only_blocks_itself(self):
        """泄露节点 1 只禁用 1 本身；根 0 仍可用，泄露节点的后代叶子也不受牵连。"""
        costs = {v: 10 for v in ALL_NODES}
        costs[0] = 5
        plan = plan_envelopes(self.tree, LEAVES, LEAVES, [1], costs)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.node_sequence, (0,))
        self.assertEqual(plan.total_cost, 5)
        self.assertNotIn(1, plan.node_sequence)
        self._assert_matches_brute_force(LEAVES, LEAVES, [1], costs, None, plan)

    def test_tie_break_lexicographic_minimum(self):
        """成本平局：根与四个叶子同价时，字典序最小序列是 (0,)。"""
        costs = {v: 2 for v in ALL_NODES}
        plan = plan_envelopes(self.tree, LEAVES, LEAVES, [], costs)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.node_sequence, (0,))
        self.assertEqual(plan.total_cost, 2)
        self._assert_matches_brute_force(LEAVES, LEAVES, [], costs, None, plan)

    def test_tie_break_between_two_node_solutions(self):
        """成本平局在两个双节点方案之间：取字典序 (1, 2)。"""
        costs = {v: 100 for v in ALL_NODES}
        costs[1] = 3
        costs[2] = 3  # (1,2)=6；根昂贵
        costs[0] = 100
        plan = plan_envelopes(self.tree, LEAVES, LEAVES, [], costs)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.node_sequence, (1, 2))
        self.assertEqual(plan.total_cost, 6)
        self._assert_matches_brute_force(LEAVES, LEAVES, [], costs, None, plan)

    def test_zero_cost_boundary(self):
        """边界：零成本节点合法，优先形成最短（字典序最小）覆盖。"""
        costs = {v: 0 for v in ALL_NODES}
        plan = plan_envelopes(self.tree, LEAVES, LEAVES, [], costs)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.total_cost, 0)
        self.assertEqual(plan.node_sequence, (0,))
        self._assert_matches_brute_force(LEAVES, LEAVES, [], costs, None, plan)

    def test_empty_members_returns_empty_plan(self):
        """边界：没有在册成员时输出空信封清单、成本为 0。"""
        costs = {v: 1 for v in ALL_NODES}
        plan = plan_envelopes(self.tree, [], LEAVES, [], costs)
        self.assertFalse(plan.uncovered_leaves)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.envelopes, ())
        self.assertEqual(plan.total_cost, 0)
        self.assertEqual(plan.covered_leaves, ())
        self._assert_matches_brute_force([], LEAVES, [], costs, None, plan)

    def test_envelope_recipients_exclude_non_holders(self):
        """交错：子树内有叶子但它不持钥时，信封接收者中不出现该叶子。"""
        # 全员在册且持钥，仅叶子 6 不持钥（既非成员也非离组者）。
        members = [3, 4, 5]
        holders = [3, 4, 5]
        costs = {v: 1 for v in ALL_NODES}
        plan = plan_envelopes(self.tree, members, holders, [], costs)
        self.assertTrue(plan.feasible)
        root_env = next(e for e in plan.envelopes if e.node == 0)
        # 根信封的接收者只包含子树内的持钥叶子，非持钥叶子 6 被排除。
        self.assertEqual(root_env.recipients, (3, 4, 5))
        self.assertNotIn(6, root_env.recipients)
        # 接收者集合与投递模拟结果一致。
        self.assertEqual(
            simulate_delivery(plan, self.tree, holders)[0], [3, 4, 5]
        )

    def test_plan_to_dict_is_serializable(self):
        """输出可模拟：结果可转成普通字典结构。"""
        costs = {v: 1 for v in ALL_NODES}
        plan = plan_envelopes(self.tree, LEAVES, LEAVES, [], costs)
        data = plan.to_dict()
        self.assertTrue(data["feasible"])
        self.assertEqual(data["envelopes"][0]["node"], 0)
        self.assertEqual(data["envelopes"][0]["recipients"], list(LEAVES))
        self.assertEqual(data["covered_leaves"], LEAVES)

    def _assert_matches_brute_force(self, members, holders, leaked, costs, leavers, plan):
        feasible, best = brute_force_best(
            self.tree, members, holders, leaked, costs, leavers
        )
        self.assertTrue(feasible)
        best_cost, best_seq = best
        self.assertEqual(plan.total_cost, best_cost)
        self.assertEqual(plan.node_sequence, best_seq)
        # 交叉约束：每个在册成员至少能打开一个信封。
        openers = simulate_delivery(plan, self.tree, holders)
        can_open = {leaf for leaves in openers.values() for leaf in leaves}
        self.assertTrue(set(members) <= can_open)
        # 交叉约束：离组者一个信封都打不开。
        leaver_set = (
            set(holders) - set(members) if leavers is None else set(leavers)
        )
        self.assertTrue(leaver_set.isdisjoint(can_open))


class RejectedAndInfeasibleTests(unittest.TestCase):
    def setUp(self):
        self.tree = build_tree()

    def test_member_missing_leaf_key_is_infeasible(self):
        """应被阻止：在册叶子 4 不持钥 -> 明确指出该叶子及原因。"""
        members = [3, 4, 5, 6]
        holders = [3, 5, 6]
        costs = {v: 1 for v in ALL_NODES}
        plan = plan_envelopes(self.tree, members, holders, [], costs)
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.envelopes, ())
        self.assertIsNone(plan.total_cost)
        self.assertEqual([u.leaf for u in plan.uncovered_leaves], [4])
        uncovered = plan.uncovered_leaves[0]
        self.assertEqual(uncovered.reason, "missing_leaf_key")
        self.assertIn("持钥", uncovered.detail)

    def test_all_path_nodes_blocked_is_infeasible(self):
        """应被阻止：叶子 3 的路径（3,1,0）被泄露+离组者全部封死。"""
        members = [3, 4]
        holders = [3, 4, 6]  # 6 离组，阻断 2 与根 0
        leaked = [1, 3]      # 1 与 3 泄露
        costs = {v: 1 for v in ALL_NODES}
        plan = plan_envelopes(self.tree, members, holders, leaked, costs)
        self.assertFalse(plan.feasible)
        leaves = {u.leaf: u for u in plan.uncovered_leaves}
        self.assertIn(3, leaves)
        self.assertEqual(leaves[3].reason, "no_safe_envelope")
        self.assertIn("泄露", leaves[3].detail)
        self.assertIn("离组", leaves[3].detail)
        # 叶子 4 自身节点未泄露也不在离组子树，可由自身信封覆盖。
        self.assertNotIn(4, leaves)

    def test_leaked_leaf_can_still_use_ancestor(self):
        """交错：仅叶子钥 3 泄露时，祖先信封仍可覆盖成员 3。"""
        costs = {v: 10 for v in ALL_NODES}
        costs[0] = 1
        plan = plan_envelopes(self.tree, LEAVES, LEAVES, [3], costs)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.node_sequence, (0,))

    def test_unavailable_node_via_missing_cost(self):
        """未给成本的节点视为不可用；方案绕行到其他节点。"""
        costs = {v: 1 for v in ALL_NODES if v != 0}  # 根不可用
        plan = plan_envelopes(self.tree, LEAVES, LEAVES, [], costs)
        self.assertTrue(plan.feasible)
        self.assertNotIn(0, plan.node_sequence)
        self.assertEqual(set(plan.node_sequence), {1, 2})

    def test_all_nodes_unavailable_reports_leaf(self):
        """路径上没有任何可用节点 -> 逐叶报告 no_safe_envelope。"""
        plan = plan_envelopes(self.tree, LEAVES, LEAVES, [], {})
        self.assertFalse(plan.feasible)
        self.assertEqual(
            sorted(u.leaf for u in plan.uncovered_leaves), sorted(LEAVES)
        )
        self.assertTrue(
            all(u.reason == "no_safe_envelope" for u in plan.uncovered_leaves)
        )

    def test_duplicate_inputs_are_deduped(self):
        """边界：重复编号按集合处理，不影响结果。"""
        costs = {v: 1 for v in ALL_NODES}
        plan = plan_envelopes(
            self.tree, [3, 3, 4, 5, 6], [3, 4, 5, 6, 6], [], costs
        )
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.covered_leaves, tuple(LEAVES))


class InputValidationTests(unittest.TestCase):
    def setUp(self):
        self.tree = build_tree()

    def test_multiple_roots_rejected(self):
        with self.assertRaises(InputError):
            DistributionTree({0: None, 1: None})

    def test_unknown_parent_rejected(self):
        with self.assertRaises(InputError):
            DistributionTree({0: None, 1: 9})

    def test_cycle_rejected(self):
        with self.assertRaises(InputError):
            DistributionTree({0: 1, 1: 0})

    def test_empty_tree_rejected(self):
        with self.assertRaises(InputError):
            DistributionTree({})

    def test_unknown_member_rejected(self):
        costs = {v: 1 for v in ALL_NODES}
        with self.assertRaises(InputError):
            plan_envelopes(self.tree, [99], LEAVES, [], costs)

    def test_internal_node_in_members_rejected(self):
        costs = {v: 1 for v in ALL_NODES}
        with self.assertRaises(InputError):
            plan_envelopes(self.tree, [1], LEAVES, [], costs)

    def test_unknown_leaked_node_rejected(self):
        costs = {v: 1 for v in ALL_NODES}
        with self.assertRaises(InputError):
            plan_envelopes(self.tree, LEAVES, LEAVES, [99], costs)

    def test_unknown_cost_node_rejected(self):
        with self.assertRaises(InputError):
            plan_envelopes(self.tree, LEAVES, LEAVES, [], {99: 1})

    def test_negative_cost_rejected(self):
        costs = {v: 1 for v in ALL_NODES}
        costs[0] = -1
        with self.assertRaises(InputError):
            plan_envelopes(self.tree, LEAVES, LEAVES, [], costs)

    def test_non_numeric_cost_rejected(self):
        costs = {v: 1 for v in ALL_NODES}
        costs[0] = "1"
        with self.assertRaises(InputError):
            plan_envelopes(self.tree, LEAVES, LEAVES, [], costs)

    def test_leaver_member_overlap_rejected(self):
        costs = {v: 1 for v in ALL_NODES}
        with self.assertRaises(InputError):
            plan_envelopes(self.tree, LEAVES, LEAVES, [], costs, leavers=[3])


class CrossCheckAcrossScenariosTests(unittest.TestCase):
    """多场景枚举交叉校验：DP 与朴素枚举在各种交错约束下结论一致。"""

    def setUp(self):
        self.tree = build_tree()

    def test_brute_force_matrix(self):
        scenarios = [
            dict(members=LEAVES, holders=LEAVES, leaked=[], leavers=None),
            dict(
                members=[3, 4, 5],
                holders=[3, 4, 5, 6],
                leaked=[],
                leavers=None,
            ),
            dict(members=LEAVES, holders=LEAVES, leaked=[1], leavers=None),
            dict(
                members=[3, 4],
                holders=[3, 4, 5, 6],
                leaked=[2],
                leavers=None,
            ),
            dict(
                members=[3, 5],
                holders=[3, 4, 5, 6],
                leaked=[0, 2],
                leavers=None,
            ),
            dict(
                members=[3, 4, 5],
                holders=[3, 4, 5],
                leaked=[],
                leavers=[6],
            ),
        ]
        cost_vectors = [
            {v: 1 for v in ALL_NODES},
            {v: idx + 1 for idx, v in enumerate(ALL_NODES)},
            {**{v: 100 for v in ALL_NODES}, 0: 5, 1: 2, 2: 2},
            {v: 0 for v in ALL_NODES},
        ]
        for sc in scenarios:
            for costs in cost_vectors:
                with self.subTest(scenario=sc, costs=costs):
                    plan = plan_envelopes(
                        self.tree,
                        sc["members"],
                        sc["holders"],
                        sc["leaked"],
                        costs,
                        sc["leavers"],
                    )
                    feasible, best = brute_force_best(
                        self.tree,
                        sc["members"],
                        sc["holders"],
                        sc["leaked"],
                        costs,
                        sc["leavers"],
                    )
                    self.assertEqual(plan.feasible, feasible)
                    if feasible:
                        self.assertEqual(plan.total_cost, best[0])
                        self.assertEqual(plan.node_sequence, best[1])
                        self.assertEqual(
                            sum(e.cost for e in plan.envelopes),
                            plan.total_cost,
                        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
