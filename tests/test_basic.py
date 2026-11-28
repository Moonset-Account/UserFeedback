"""公开 API 的基础行为与手工算例。"""

import unittest

from consistency import (
    ConsistencyError,
    Node,
    Reconciliation,
    RegionTree,
    brute_force_optimal,
    reconcile,
)


class TestRegionTreeBuild(unittest.TestCase):
    def test_basic_structure_and_leaf_order(self):
        a1 = Node("a1", 3, 1)
        a2 = Node("a2", 5, 1)
        a = Node("A", 7, 1, children=[a1, a2])
        b1 = Node("b1", 0, 1, structural_zero=True)
        b = Node("B", 2, 1, children=[b1])
        root = Node("city", 10, 4, children=[a, b])
        tree = RegionTree.build(root)

        self.assertEqual([lf.node_id for lf in tree.leaves], ["a1", "a2", "b1"])
        self.assertEqual(tree.depth["city"], 0)
        self.assertEqual(tree.depth["b1"], 2)
        self.assertIsNone(tree.parent["city"])
        self.assertEqual(tree.parent["a1"].node_id, "A")
        self.assertEqual(set(tree.nodes), {"city", "A", "B", "a1", "a2", "b1"})

    def test_duplicate_id_rejected(self):
        root = Node(
            "r", 2, 1, children=[Node("x", 1, 1), Node("x", 1, 1)]
        )
        with self.assertRaises(ValueError):
            RegionTree.build(root)

    def test_empty_id_rejected(self):
        with self.assertRaises(ValueError):
            RegionTree.build(Node("", 1, 1))

    def test_non_positive_weight_rejected(self):
        with self.assertRaises(ValueError):
            RegionTree.build(Node("r", 1, 0))
        with self.assertRaises(ValueError):
            RegionTree.build(Node("r", 1, -2))

    def test_bool_rejected_as_int(self):
        with self.assertRaises(TypeError):
            RegionTree.build(Node("r", True, 1))
        with self.assertRaises(TypeError):
            RegionTree.build(Node("r", 1, True))

    def test_noisy_may_be_negative(self):
        tree = RegionTree.build(
            Node("r", 1, 1, children=[Node("x", -5, 2), Node("y", 3, 1)])
        )
        result = reconcile(tree)
        self.assertEqual(result.counts["r"], 1)
        self.assertGreaterEqual(result.counts["x"], 0)
        self.assertGreaterEqual(result.counts["y"], 0)

    def test_structural_zero_only_on_leaf(self):
        bad = Node("r", 1, 1, structural_zero=True, children=[Node("x", 0, 1)])
        with self.assertRaises(ValueError):
            RegionTree.build(bad)

    def test_bad_node_type(self):
        with self.assertRaises(TypeError):
            RegionTree.build("not a node")  # type: ignore[arg-type]
        root = Node("r", 1, 1, children=["bad"])  # type: ignore[list-item]
        with self.assertRaises(TypeError):
            RegionTree.build(root)


class TestHandWorkedExamples(unittest.TestCase):
    def test_two_leaves_identical_weight_split(self):
        # 根 T=6，两叶子带噪 1/1、权重相同；边际相同 -> 字典序小 => 后位多分
        tree = RegionTree.build(
            Node("r", 6, 1, children=[Node("l1", 1, 1), Node("l2", 1, 1)])
        )
        result = reconcile(tree)
        self.assertEqual(result.counts, {"r": 6, "l1": 3, "l2": 3})
        self.assertEqual(result.total_cost, (6 - 6) ** 2 + 4 + 4)

    def test_tie_break_lexicographic_uneven(self):
        # T=5，两叶子完全对称；字典序最小的最优向量是 (2, 3)
        tree = RegionTree.build(
            Node("r", 5, 1, children=[Node("l1", 0, 1), Node("l2", 0, 1)])
        )
        result = reconcile(tree)
        self.assertEqual(result.counts["l1"], 2)
        self.assertEqual(result.counts["l2"], 3)
        # 穷举给出的最优解必须一致
        self.assertEqual(reconcile(tree), brute_force_optimal(tree))

    def test_three_leaves_tie_break(self):
        tree = RegionTree.build(
            Node(
                "r",
                4,
                1,
                children=[Node("a", 0, 1), Node("b", 0, 1), Node("c", 0, 1)],
            )
        )
        result = reconcile(tree)
        # 对称情形下字典序最小的 4 的拆分是 (1, 1, 2)
        self.assertEqual(
            [result.counts[x] for x in result.leaf_order], [1, 1, 2]
        )
        self.assertEqual(reconcile(tree), brute_force_optimal(tree))

    def test_weighted_split_prefers_high_weight_target(self):
        # l1 权重 5 且带噪 4，l2 权重 1 且带噪 0，T=4
        tree = RegionTree.build(
            Node("r", 4, 1, children=[Node("l1", 4, 5), Node("l2", 0, 1)])
        )
        result = reconcile(tree)
        self.assertEqual(result.counts["l1"], 4)
        self.assertEqual(result.counts["l2"], 0)
        self.assertEqual(result.total_cost, 0 + 0 + 0)
        self.assertEqual(reconcile(tree), brute_force_optimal(tree))

    def test_structural_zero_leaf_stays_zero(self):
        z = Node("z", 99, 7, structural_zero=True)
        tree = RegionTree.build(
            Node("r", 3, 1, children=[Node("a", 2, 1), z])
        )
        result = reconcile(tree)
        self.assertEqual(result.counts["z"], 0)
        self.assertEqual(result.counts["a"], 3)
        self.assertEqual(result.counts["r"], 3)
        # 结构性零节点仍参与代价（其带噪值与最终 0 之间的改动要计费）
        self.assertIn("z", result.counts)
        self.assertEqual(reconcile(tree), brute_force_optimal(tree))

    def test_motivating_county_sum_exceeds_city(self):
        # 题面场景：区县之和（带噪）大于全市，校正后必须父=子之和、根固定
        c1 = Node("c1", 60, 1)
        c2 = Node("c2", 70, 1)
        root = Node("city", 100, 2, children=[c1, c2])
        tree = RegionTree.build(root)
        result = reconcile(tree)
        self.assertEqual(result.counts["city"], 100)
        self.assertEqual(
            result.counts["c1"] + result.counts["c2"],
            result.counts["city"],
        )
        self.assertEqual(reconcile(tree), brute_force_optimal(tree))

    def test_total_zero_forces_all_zero(self):
        tree = RegionTree.build(
            Node(
                "r",
                0,
                1,
                children=[
                    Node("a", 5, 2, children=[Node("a1", 3, 1)]),
                    Node("b", -4, 1),
                ],
            )
        )
        result = reconcile(tree)
        for value in result.counts.values():
            self.assertEqual(value, 0)
        self.assertEqual(
            result.total_cost,
            1 * (0 - 0) ** 2
            + 2 * (0 - 5) ** 2
            + 1 * (0 - 3) ** 2
            + 1 * (0 - (-4)) ** 2,
        )
        self.assertEqual(reconcile(tree), brute_force_optimal(tree))

    def test_single_leaf_takes_everything(self):
        tree = RegionTree.build(
            Node("r", 7, 3, children=[Node("only", 2, 5)])
        )
        result = reconcile(tree)
        self.assertEqual(result.counts["only"], 7)
        self.assertEqual(result.counts["r"], 7)
        self.assertEqual(
            result.total_cost,
            3 * (7 - 7) ** 2 + 5 * (7 - 2) ** 2,
        )

    def test_result_is_reconciliation_dataclass(self):
        tree = RegionTree.build(Node("r", 1, 1, children=[Node("x", 1, 1)]))
        result = reconcile(tree)
        self.assertIsInstance(result, Reconciliation)
        self.assertEqual(result.structural_zeros, frozenset())
        self.assertEqual(result.leaf_order, ["x"])


class TestInfeasibleAndErrors(unittest.TestCase):
    def test_negative_root_total(self):
        tree = RegionTree.build(
            Node("r", -1, 1, children=[Node("x", 0, 1)])
        )
        with self.assertRaises(ConsistencyError):
            reconcile(tree)
        with self.assertRaises(ConsistencyError):
            brute_force_optimal(tree)

    def test_all_structural_zero_with_positive_total(self):
        tree = RegionTree.build(
            Node(
                "r",
                3,
                1,
                children=[
                    Node("a", 0, 1, structural_zero=True),
                    Node("b", 0, 1, structural_zero=True),
                ],
            )
        )
        with self.assertRaises(ConsistencyError):
            reconcile(tree)

    def test_all_structural_zero_with_zero_total_is_feasible(self):
        tree = RegionTree.build(
            Node(
                "r",
                0,
                1,
                children=[
                    Node("a", 2, 1, structural_zero=True),
                    Node("b", 3, 1, structural_zero=True),
                ],
            )
        )
        result = reconcile(tree)
        self.assertEqual(set(result.counts.values()), {0})

    def test_brute_force_guard_raises(self):
        leaves = [Node(f"l{i}", 0, 1) for i in range(4)]
        tree = RegionTree.build(Node("r", 100, 1, children=leaves))
        # C(103,3) = 176851 > 上限 1000
        with self.assertRaises(ValueError):
            brute_force_optimal(tree, max_combinations=1000)

    def test_wrong_type_to_reconcile(self):
        with self.assertRaises(TypeError):
            reconcile("nope")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
