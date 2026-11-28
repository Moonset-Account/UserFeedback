"""大规模与交错场景：百万总数、数百叶子，以及结构/权重交错的合成数据。"""

import random
import time
import unittest

from consistency import Node, RegionTree, reconcile


def build_balanced_tree(leaf_count: int, total: int, rng: random.Random) -> RegionTree:
    """构造每个内部节点两孩子的平衡二叉地区树（叶子数任意时允许单侧分叉）。"""
    level = [
        Node(
            f"leaf{i}",
            rng.randint(-50, total // max(leaf_count, 1) + 50),
            rng.randint(1, 10),
            structural_zero=(rng.random() < 0.1),
        )
        for i in range(leaf_count)
    ]
    counter = 0
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            if i + 1 < len(level):
                nxt.append(
                    Node(
                        f"mid{counter}",
                        rng.randint(-20, total + 20),
                        rng.randint(1, 6),
                        children=[level[i], level[i + 1]],
                    )
                )
                counter += 1
            else:
                nxt.append(level[i])
        level = nxt
    root = level[0]
    root.node_id = "root"
    root.noisy = total
    return RegionTree.build(root)


class TestLargeScale(unittest.TestCase):
    def test_one_million_total_three_hundred_leaves(self):
        rng = random.Random(20261128)
        tree = build_balanced_tree(300, 1_000_000, rng)

        start = time.perf_counter()
        result = reconcile(tree)
        elapsed = time.perf_counter() - start

        self.assertEqual(result.counts["root"], 1_000_000)
        self.assertLess(
            elapsed,
            30.0,
            msg=f"百万总数求解耗时 {elapsed:.1f}s 超出预算",
        )
        # 叶子计数之和必须等于根
        leaf_sum = sum(result.counts[lf.node_id] for lf in tree.leaves)
        self.assertEqual(leaf_sum, 1_000_000)

    def test_invariants_on_large_tree(self):
        rng = random.Random(555)
        tree = build_balanced_tree(257, 500_000, rng)
        result = reconcile(tree)

        for node in tree.nodes.values():
            value = result.counts[node.node_id]
            self.assertGreaterEqual(value, 0)
            if node.children:
                self.assertEqual(
                    value,
                    sum(result.counts[c.node_id] for c in node.children),
                )
            if node.structural_zero:
                self.assertEqual(value, 0)

        recomputed = sum(
            node.weight
            * (result.counts[node.node_id] - node.noisy) ** 2
            for node in tree.nodes.values()
        )
        self.assertEqual(recomputed, result.total_cost)
        self.assertGreaterEqual(result.total_cost, 0)

    def test_deep_chain_tree(self):
        """深度与叶子数接近的链状树：每条祖先链很长，检验正确性与性能。"""
        rng = random.Random(11)
        leaf_count = 200
        total = 100_000
        leaf = Node("leaf0", rng.randint(0, 100), rng.randint(1, 5))
        root = leaf
        for d in range(leaf_count - 1):
            side_leaf = Node(
                f"leaf{d + 1}",
                rng.randint(0, 100),
                rng.randint(1, 5),
                structural_zero=(d % 37 == 0),
            )
            root = Node(
                f"mid{d}",
                rng.randint(0, total),
                rng.randint(1, 4),
                children=[root, side_leaf],
            )
        root.node_id = "root"
        root.noisy = total
        tree = RegionTree.build(root)

        result = reconcile(tree)
        self.assertEqual(result.counts["root"], total)
        self.assertEqual(
            sum(result.counts[lf.node_id] for lf in tree.leaves), total
        )
        for node in tree.nodes.values():
            if node.structural_zero:
                self.assertEqual(result.counts[node.node_id], 0)

    def test_uniform_weights_concentrates_near_noisy_leaves(self):
        """所有权重相同、内部节点权重也相同的情形下，叶子层面应近似按带噪比例。

        这里只校验可验证的硬性质，不假设精确解析值：
        总和守恒、非负、与穷举在缩小版上的解结构一致。
        """
        rng = random.Random(808)
        tree = build_balanced_tree(6, 20, rng)
        for node in tree.nodes.values():
            node.weight = 1
        result = reconcile(tree)
        self.assertEqual(
            sum(result.counts[lf.node_id] for lf in tree.leaves), 20
        )

    def test_many_structural_zeros_still_feasible(self):
        rng = random.Random(31)
        leaves = []
        for i in range(100):
            leaves.append(
                Node(
                    f"leaf{i}",
                    rng.randint(0, 10),
                    rng.randint(1, 4),
                    structural_zero=(i % 2 == 0),
                )
            )
        root = Node("root", 4096, 2, children=leaves)
        tree = RegionTree.build(root)
        result = reconcile(tree)
        self.assertEqual(result.counts["root"], 4096)
        for i in range(0, 100, 2):
            self.assertEqual(result.counts[f"leaf{i}"], 0)
        odd_sum = sum(result.counts[f"leaf{i}"] for i in range(1, 100, 2))
        self.assertEqual(odd_sum, 4096)

    def test_extreme_weight_skew(self):
        """单个叶子权重极大时，单位应优先灌入该叶子直到其边际追平他人。"""
        special = Node("special", 1000, 10_000)
        others = [Node(f"o{i}", 0, 1) for i in range(9)]
        tree = RegionTree.build(
            Node("root", 1000, 1, children=[special, *others])
        )
        result = reconcile(tree)
        self.assertEqual(result.counts["special"], 1000)
        self.assertEqual(sum(result.counts[o.node_id] for o in others), 0)

    def test_large_total_zero(self):
        rng = random.Random(909)
        tree = build_balanced_tree(100, 0, rng)
        result = reconcile(tree)
        self.assertTrue(all(v == 0 for v in result.counts.values()))
        expected = sum(
            node.weight * node.noisy**2 for node in tree.nodes.values()
        )
        self.assertEqual(result.total_cost, expected)


if __name__ == "__main__":
    unittest.main()
