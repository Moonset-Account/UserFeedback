"""补充实测：性能计时 + 字典序平局规则的穷举独立性核对。"""

import random
import time

from consistency import Node, RegionTree, brute_force_optimal, reconcile


def build_balanced(leaf_count, total, rng, zero_prob=0.1):
    zero_flags = [rng.random() < zero_prob for _ in range(leaf_count)]
    if all(zero_flags):
        zero_flags[rng.randrange(leaf_count)] = False
    level = [
        Node(
            f"leaf{i}",
            rng.randint(-50, total // max(leaf_count, 1) + 50),
            rng.randint(1, 10),
            structural_zero=zero_flags[i],
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


def main():
    rng = random.Random(20261128)

    print("== 性能：总数 1,000,000、300 叶子 ==")
    for leaves, total in [(300, 1_000_000), (500, 1_000_000), (100, 1_000_000)]:
        tree = build_balanced(leaves, total, rng)
        t0 = time.perf_counter()
        result = reconcile(tree)
        dt = time.perf_counter() - t0
        leaf_sum = sum(result.counts[lf.node_id] for lf in tree.leaves)
        print(
            f"  leaves={leaves:4d} T={total:>9,}  耗时 {dt:6.2f}s  "
            f"叶子和={leaf_sum:,}  代价={result.total_cost}"
        )
        assert leaf_sum == total
        assert result.counts["root"] == total

    print()
    print("== 字典序平局：构造存在多组同代价最优解的实例，与穷举逐一比对 ==")
    # 完全对称的两叶子（同 noisy、同权重、根同权重）必然产生平局
    cases = 0
    for total in range(0, 9):
        tree = RegionTree.build(
            Node(
                "r",
                total,
                1,
                children=[Node("l1", 2, 1), Node("l2", 2, 1)],
            )
        )
        g, e = reconcile(tree), brute_force_optimal(tree)
        assert g == e, (total, g, e)
        vec_g = [g.counts["l1"], g.counts["l2"]]
        print(f"  T={total}: 贪心/穷举叶子向量 = {vec_g}")
        cases += 1

    print()
    print("== 随机穷举等价性（更大轮次，独立随机种子） ==")
    rng2 = random.Random(987654321)
    mismatches = 0
    for trial in range(150):
        tree = build_balanced(rng.randint(2, 6), rng2.randint(0, 7), rng2, zero_prob=0.3)
        g, e = reconcile(tree), brute_force_optimal(tree, max_combinations=100_000)
        if g != e:
            mismatches += 1
            print("  MISMATCH", trial)
    print(f"  150 组随机小树，不一致数 = {mismatches}")
    assert mismatches == 0
    print(f"  共核对 {cases + 150} 组，全部一致")


if __name__ == "__main__":
    main()
