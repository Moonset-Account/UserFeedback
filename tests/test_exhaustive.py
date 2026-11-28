"""随机合成小树：贪心解与穷举全局最优逐例核对（含字典序平局规则）。"""

import random
import unittest

from consistency import (
    Node,
    RegionTree,
    brute_force_optimal,
    reconcile,
)


def make_random_tree(rng: random.Random, leaf_count: int, total: int) -> Node:
    """随机生成一棵含 leaf_count 个叶子的树。

    内部节点通过反复随机合并节点列表构造，保证每个内部节点至少有两个孩子；
    noisy/weight/structural_zero 也随机生成（根的 noisy 固定为 total）。
    """
    nodes: list[Node] = []
    zero_flags = [rng.random() < 0.25 for _ in range(leaf_count)]
    # 保证至少有一个可调节叶子，否则正总数下问题不可行
    if all(zero_flags):
        zero_flags[rng.randrange(leaf_count)] = False
    for i in range(leaf_count):
        nodes.append(
            Node(
                f"leaf{i}",
                rng.randint(-3, 6),
                rng.randint(1, 5),
                structural_zero=zero_flags[i],
            )
        )

    counter = 0
    while len(nodes) > 1:
        k = min(len(nodes), rng.randint(2, 3))
        rng.shuffle(nodes)
        children = nodes[:k]
        rest = nodes[k:]
        internal = Node(
            f"n{counter}",
            rng.randint(-2, total + 3),
            rng.randint(1, 4),
            children=children,
        )
        counter += 1
        nodes = rest + [internal]

    root = nodes[0]
    root.node_id = "root"
    root.noisy = total
    return root


class TestExhaustiveEquivalence(unittest.TestCase):
    def test_random_small_trees_match_exhaustive(self):
        rng = random.Random(20261128)
        checked = 0
        for trial in range(80):
            leaf_count = rng.randint(2, 6)
            total = rng.randint(0, 8)
            root = make_random_tree(rng, leaf_count, total)
            tree = RegionTree.build(root)

            greedy = reconcile(tree)
            exact = brute_force_optimal(tree, max_combinations=50_000)

            self.assertEqual(
                greedy,
                exact,
                msg=f"第 {trial} 组随机树贪心与穷举不一致",
            )
            checked += 1
        self.assertEqual(checked, 80)

    def test_determinism_repeated_calls(self):
        rng = random.Random(42)
        root = make_random_tree(rng, 5, 7)
        tree = RegionTree.build(root)
        first = reconcile(tree)
        for _ in range(5):
            self.assertEqual(reconcile(tree), first)

    def test_invariants_on_random_trees(self):
        """随机树上独立复核：非负、父=子之和、根=总数、代价独立重算一致。"""
        rng = random.Random(7)
        for _ in range(40):
            leaf_count = rng.randint(2, 8)
            total = rng.randint(0, 12)
            tree = RegionTree.build(make_random_tree(rng, leaf_count, total))
            result = reconcile(tree)

            for node_id, value in result.counts.items():
                self.assertGreaterEqual(value, 0)
                node = tree.nodes[node_id]
                if node.children:
                    self.assertEqual(
                        value,
                        sum(result.counts[c.node_id] for c in node.children),
                        msg="父节点必须等于孩子之和",
                    )
            self.assertEqual(result.counts[tree.root.node_id], total)

            for leaf in tree.leaves:
                if leaf.structural_zero:
                    self.assertEqual(result.counts[leaf.node_id], 0)

            recomputed = sum(
                node.weight
                * (result.counts[node.node_id] - node.noisy) ** 2
                for node in tree.nodes.values()
            )
            self.assertEqual(recomputed, result.total_cost)

    def test_structural_zeros_present_in_random_trees(self):
        """确保随机序列中确实出现过结构性零，避免该分支从未被覆盖。"""
        rng = random.Random(99)
        saw_zero = False
        for _ in range(30):
            tree = RegionTree.build(make_random_tree(rng, 5, 6))
            result = reconcile(tree)
            if result.structural_zeros:
                saw_zero = True
                for zid in result.structural_zeros:
                    self.assertEqual(result.counts[zid], 0)
        self.assertTrue(saw_zero, "随机用例应当覆盖到结构性零叶子")

    def test_all_structural_zeros_feasibility_mix(self):
        rng = random.Random(123)
        for _ in range(20):
            leaf_count = rng.randint(2, 5)
            total = rng.randint(0, 6)
            root = make_random_tree(rng, leaf_count, total)
            tree = RegionTree.build(root)
            # 生成器已保证至少一个可调节叶子，这里全部应可解且与穷举一致
            self.assertEqual(reconcile(tree), brute_force_optimal(tree))

        # 手工构造全部叶子为结构性零的交错情形
        zero_tree = RegionTree.build(
            Node(
                "r",
                0,
                1,
                children=[
                    Node("z1", 4, 1, structural_zero=True),
                    Node("z2", 1, 2, structural_zero=True),
                ],
            )
        )
        result = reconcile(zero_tree)
        self.assertEqual(result, brute_force_optimal(zero_tree))
        self.assertEqual(set(result.counts.values()), {0})


if __name__ == "__main__":
    unittest.main()
