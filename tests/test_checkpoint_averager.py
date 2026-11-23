"""checkpoint_averager 的 unittest 测试。

覆盖：正常路径、边界（W=1 / W 超过步数 / alpha 端点 / 固定尝试 / 祖先范围 /
单步清单 / 不可行清单）、迟到补登记与固定旧修订回读的交错场景，以及两个
"分叉后再合并"的小例，验证无法把不同分支各自有利的步拼成不存在的平均值。
"""

import unittest
from fractions import Fraction

from checkpoint_averager import (
    Checkpoint,
    CheckpointDirectory,
    InfeasibleManifestError,
    Manifest,
    StepRequirement,
    compute_digest,
    evaluate,
)


def ck(cid, step, parents=(), params=()):
    return Checkpoint.create(cid, step, parents, params)


def manifest(steps, window, alpha, **kw):
    reqs = tuple(
        StepRequirement(step=s, **kw.get(i, {})) for i, s in enumerate(steps)
    )
    return Manifest(requirements=reqs, window=window, alpha=Fraction(alpha))


class TestDigestAndDirectory(unittest.TestCase):
    def test_digest_deterministic_and_content_sensitive(self):
        d1 = compute_digest(3, ("a", "b"), [Fraction(1, 2)])
        d2 = compute_digest(3, ("a", "b"), [Fraction(1, 2)])
        d3 = compute_digest(3, ("a", "b"), [Fraction(1, 3)])
        self.assertEqual(d1, d2)
        self.assertNotEqual(d1, d3)

    def test_duplicate_registration_rejected(self):
        d = CheckpointDirectory()
        d.register(ck("x", 1, params=[1]))
        with self.assertRaises(ValueError):
            d.register(ck("x", 1, params=[2]))

    def test_duplicate_parent_rejected(self):
        with self.assertRaises(ValueError):
            ck("x", 1, parents=("p", "p"), params=[1])

    def test_late_registration_out_of_step_order(self):
        """迟到补登记：先登记后面的步，再补登前面的步。"""
        d = CheckpointDirectory()
        d.register(ck("s2", 2, parents=("s1",), params=[3]))
        self.assertEqual(d.at_step(1), [])  # s1 尚未登记
        d.register(ck("s1", 1, params=[1]))  # 迟到补登记
        self.assertTrue(d.is_reachable("s1", "s2"))
        self.assertEqual([c.checkpoint_id for c in d.at_step(1)], ["s1"])

    def test_fixed_old_revision_readback(self):
        """固定旧修订回读：旧修订看不到后来登记的尝试。"""
        d = CheckpointDirectory()
        r1 = d.register(ck("a", 1, params=[1]))
        r2 = d.register(ck("b", 1, params=[2]))
        self.assertLess(r1, r2)
        self.assertEqual([c.checkpoint_id for c in d.at_step(1, revision=r1)], ["a"])
        self.assertEqual(
            [c.checkpoint_id for c in d.at_step(1, revision=r2)], ["a", "b"]
        )
        self.assertIsNone(d.get("b", revision=r1))
        self.assertIsNotNone(d.get("b", revision=r2))


class TestEvaluateNormal(unittest.TestCase):
    def test_single_chain_unique_value_publishable(self):
        """正常场景：唯一链，各维 min == max，可发布。"""
        d = CheckpointDirectory()
        d.register(ck("c1", 1, params=[1, 2]))
        d.register(ck("c2", 2, parents=("c1",), params=[3, 4]))
        d.register(ck("c3", 3, parents=("c2",), params=[5, 6]))
        m = manifest([1, 2, 3], window=2, alpha=Fraction(1, 2))
        res = evaluate(d, m)
        # 窗口 [c2, c3]：avg = (c2 + c3) / 2
        self.assertTrue(res.publishable)
        self.assertEqual(res.window_steps, (2, 3))
        self.assertEqual(res.dimensions[0].minimum, Fraction(4))
        self.assertEqual(res.dimensions[1].minimum, Fraction(5))
        wit = res.dimensions[0].min_witness
        self.assertEqual(wit.chosen, ("c1", "c2", "c3"))
        self.assertEqual(wit.path, ("c1", "c2", "c3"))

    def test_window_first_term_is_initial_value(self):
        """顺序相关：窗口首项作为初值，系数 alpha 作用于后续项。"""
        d = CheckpointDirectory()
        d.register(ck("c1", 1, params=[0]))
        d.register(ck("c2", 2, parents=("c1",), params=[10]))
        d.register(ck("c3", 3, parents=("c2",), params=[20]))
        m = manifest([1, 2, 3], window=3, alpha=Fraction(1, 2))
        res = evaluate(d, m)
        # s = 0; s = (0+10)/2 = 5; s = (5+20)/2 = 25/2
        self.assertEqual(res.dimensions[0].minimum, Fraction(25, 2))
        self.assertEqual(res.window_steps, (1, 2, 3))

    def test_window_larger_than_steps_is_clamped(self):
        d = CheckpointDirectory()
        d.register(ck("c1", 1, params=[2]))
        d.register(ck("c2", 2, parents=("c1",), params=[4]))
        m = manifest([1, 2], window=99, alpha=Fraction(1, 2))
        res = evaluate(d, m)
        self.assertEqual(res.window_steps, (1, 2))
        self.assertEqual(res.dimensions[0].minimum, Fraction(3))

    def test_window_one_takes_last_value(self):
        d = CheckpointDirectory()
        d.register(ck("c1", 1, params=[7]))
        d.register(ck("c2", 2, parents=("c1",), params=[9]))
        m = manifest([1, 2], window=1, alpha=Fraction(1, 3))
        res = evaluate(d, m)
        self.assertEqual(res.dimensions[0].minimum, Fraction(9))
        self.assertEqual(res.window_steps, (2,))

    def test_alpha_zero_keeps_first_window_term(self):
        d = CheckpointDirectory()
        d.register(ck("c1", 1, params=[5]))
        d.register(ck("c2", 2, parents=("c1",), params=[8]))
        m = manifest([1, 2], window=2, alpha=Fraction(0))
        res = evaluate(d, m)
        self.assertEqual(res.dimensions[0].minimum, Fraction(5))

    def test_alpha_one_keeps_last_window_term(self):
        d = CheckpointDirectory()
        d.register(ck("c1", 1, params=[5]))
        d.register(ck("c2", 2, parents=("c1",), params=[8]))
        m = manifest([1, 2], window=2, alpha=Fraction(1))
        res = evaluate(d, m)
        self.assertEqual(res.dimensions[0].minimum, Fraction(8))

    def test_fixed_attempt(self):
        d = CheckpointDirectory()
        d.register(ck("a", 1, params=[1]))
        d.register(ck("b", 1, params=[100]))
        d.register(ck("c", 2, parents=("a",), params=[3]))
        d.register(ck("e", 2, parents=("b",), params=[7]))
        # 固定第 0 步选 b，则第 1 步只能选 e（血缘可达）
        m = Manifest(
            requirements=(
                StepRequirement(step=1, fixed="b"),
                StepRequirement(step=2),
            ),
            window=2,
            alpha=Fraction(1, 2),
        )
        res = evaluate(d, m)
        self.assertTrue(res.publishable)
        self.assertEqual(res.dimensions[0].minimum, Fraction(107, 2))
        self.assertEqual(res.dimensions[0].min_witness.chosen, ("b", "e"))

    def test_fixed_attempt_step_mismatch_rejected(self):
        d = CheckpointDirectory()
        d.register(ck("a", 1, params=[1]))
        m = Manifest(
            requirements=(StepRequirement(step=2, fixed="a"),),
            window=1,
            alpha=Fraction(1, 2),
        )
        with self.assertRaises(ValueError):
            evaluate(d, m)

    def test_allowed_ancestors_range(self):
        """每步允许的祖先范围：只有落在允许祖先范围内的尝试可选。"""
        d = CheckpointDirectory()
        d.register(ck("root", 0, params=[0]))
        d.register(ck("a1", 1, parents=("root",), params=[1]))
        d.register(ck("b1", 1, params=[2]))  # 另一支，不经 root
        d.register(ck("a2", 2, parents=("a1",), params=[3]))
        d.register(ck("b2", 2, parents=("b1",), params=[4]))
        m = Manifest(
            requirements=(
                StepRequirement(step=1, allowed_ancestors=frozenset({"root"})),
                StepRequirement(step=2, allowed_ancestors=frozenset({"root"})),
            ),
            window=2,
            alpha=Fraction(1, 2),
        )
        res = evaluate(d, m)
        self.assertTrue(res.publishable)
        self.assertEqual(res.dimensions[0].min_witness.chosen, ("a1", "a2"))
        self.assertEqual(res.dimensions[0].minimum, Fraction(2))

    def test_single_step_manifest(self):
        d = CheckpointDirectory()
        d.register(ck("a", 5, params=[3]))
        d.register(ck("b", 5, params=[3]))  # 同值不同尝试
        m = manifest([5], window=1, alpha=Fraction(1, 2))
        res = evaluate(d, m)
        self.assertTrue(res.publishable)
        self.assertEqual(res.dimensions[0].minimum, Fraction(3))

    def test_infeasible_when_step_missing(self):
        d = CheckpointDirectory()
        d.register(ck("a", 1, params=[1]))
        with self.assertRaises(InfeasibleManifestError):
            evaluate(d, manifest([1, 2], window=1, alpha=Fraction(1, 2)))

    def test_infeasible_when_lineage_broken(self):
        """两步都有候选，但血缘上前后不可达。"""
        d = CheckpointDirectory()
        d.register(ck("a", 1, params=[1]))
        d.register(ck("b", 2, params=[2]))  # 无父链到 a
        with self.assertRaises(InfeasibleManifestError):
            evaluate(d, manifest([1, 2], window=1, alpha=Fraction(1, 2)))

    def test_manifest_validation(self):
        with self.assertRaises(ValueError):
            Manifest(requirements=(), window=1, alpha=Fraction(1, 2))
        with self.assertRaises(ValueError):
            manifest([1, 1], window=1, alpha=Fraction(1, 2))
        with self.assertRaises(ValueError):
            manifest([1], window=0, alpha=Fraction(1, 2))
        with self.assertRaises(ValueError):
            manifest([1], window=1, alpha=Fraction(3, 2))

    def test_per_dimension_extremes_from_different_paths(self):
        """多维参数：不同维度的最小值可来自不同路径，各附见证。"""
        d = CheckpointDirectory()
        d.register(ck("a", 1, params=[0, 10]))
        d.register(ck("b", 1, params=[10, 0]))
        d.register(ck("ma", 2, parents=("a",), params=[0, 0]))
        d.register(ck("mb", 2, parents=("b",), params=[0, 0]))
        m = manifest([1, 2], window=2, alpha=Fraction(1, 2))
        res = evaluate(d, m)
        self.assertFalse(res.publishable)
        d0, d1 = res.dimensions
        self.assertEqual((d0.minimum, d0.maximum), (Fraction(0), Fraction(5)))
        self.assertEqual((d1.minimum, d1.maximum), (Fraction(0), Fraction(5)))
        self.assertEqual(d0.min_witness.chosen, ("a", "ma"))
        self.assertEqual(d1.min_witness.chosen, ("b", "mb"))


class TestForkMergeSplicing(unittest.TestCase):
    """两个分叉后再合并的小例：验证不能拼接不同分支各自有利的步。"""

    def _build_example1(self):
        """例 1：A 支第 1 步有利、B 支第 2 步有利，合并后两条合法路径同值。

        步骤1: A1=(0)  B1=(10)
        步骤2: A2=(10)<-A1   B2=(0)<-B1
        步骤3: M=(5)<-(A2,B2)
        逐步最小值的拼接下界是 0/4+0/4+5/2 = 5/2，但不存在这样的合法路径；
        合法路径 A 与 B 的平均都是 5。
        """
        d = CheckpointDirectory()
        d.register(ck("A1", 1, params=[0]))
        d.register(ck("B1", 1, params=[10]))
        d.register(ck("A2", 2, parents=("A1",), params=[10]))
        d.register(ck("B2", 2, parents=("B1",), params=[0]))
        d.register(ck("M", 3, parents=("A2", "B2"), params=[5]))
        return d

    def test_example1_spliced_average_does_not_exist(self):
        d = self._build_example1()
        m = manifest([1, 2, 3], window=3, alpha=Fraction(1, 2))
        res = evaluate(d, m)
        # 权重 1/4, 1/4, 1/2。拼接下界 5/2 不可达；两条合法路径都是 5。
        self.assertEqual(res.dimensions[0].minimum, Fraction(5))
        self.assertEqual(res.dimensions[0].maximum, Fraction(5))
        self.assertNotEqual(res.dimensions[0].minimum, Fraction(5, 2))
        self.assertTrue(res.publishable)
        # 见证路径完整且共享祖先只算一次
        wit = res.dimensions[0].min_witness
        self.assertEqual(wit.chosen[-1], "M")
        self.assertEqual(len(wit.path), len(set(wit.path)))
        self.assertEqual(wit.path[-1], "M")
        self.assertIn(wit.chosen[0], ("A1", "B1"))

    def _build_example2(self):
        """例 2：分叉后合并，两条合法路径平均值不同 -> 不可发布并报分歧。

        步骤1: A1=(0)  B1=(4)
        步骤2: A2=(8)<-A1   B2=(2)<-B1
        步骤3: M=(3)<-(A2,B2)
        路径 A: 0/4 + 8/4 + 3/2 = 7/2；路径 B: 4/4 + 2/4 + 3/2 = 3。
        拼接下界 0/4 + 2/4 + 3/2 = 2 不存在。
        """
        d = CheckpointDirectory()
        d.register(ck("A1", 1, params=[0]))
        d.register(ck("B1", 1, params=[4]))
        d.register(ck("A2", 2, parents=("A1",), params=[8]))
        d.register(ck("B2", 2, parents=("B1",), params=[2]))
        d.register(ck("M", 3, parents=("A2", "B2"), params=[3]))
        return d

    def test_example2_spliced_minimum_rejected_and_divergence_reported(self):
        d = self._build_example2()
        m = manifest([1, 2, 3], window=3, alpha=Fraction(1, 2))
        res = evaluate(d, m)
        dim = res.dimensions[0]
        self.assertEqual(dim.minimum, Fraction(3))      # 不是拼接出的 2
        self.assertEqual(dim.maximum, Fraction(7, 2))
        self.assertNotEqual(dim.minimum, Fraction(2))
        self.assertFalse(res.publishable)
        # 首个分歧步：两条路径在第 1 步就不同（B1 vs A1）
        div = res.divergence
        self.assertIsNotNone(div)
        self.assertEqual(div.step, 1)
        self.assertEqual(div.position, 0)
        self.assertEqual(div.choice_min[0], "B1")
        self.assertEqual(div.choice_max[0], "A1")
        # 竞争摘要与目录中的内容摘要一致
        self.assertEqual(div.choice_min[1], d.get("B1").digest)
        self.assertEqual(div.choice_max[1], d.get("A1").digest)
        # 两条见证路径都是血缘可达的完整路径
        for wit in (dim.min_witness, dim.max_witness):
            self.assertEqual(wit.path[-1], "M")
            self.assertEqual(len(wit.path), len(set(wit.path)))

    def test_example2_merge_witness_counts_shared_ancestors_once(self):
        """合并点 M 有两条父链；见证路径中任何节点只出现一次。"""
        d = self._build_example2()
        m = manifest([1, 2, 3], window=3, alpha=Fraction(1, 2))
        res = evaluate(d, m)
        for dim in res.dimensions:
            for wit in (dim.min_witness, dim.max_witness):
                self.assertEqual(len(wit.path), len(set(wit.path)))
                # 路径沿血缘单调推进：相邻节点存在父子或祖孙关系
                for a, b in zip(wit.path, wit.path[1:]):
                    self.assertTrue(d.is_reachable(a, b))


class TestLateRegistrationAndRevisionInterleaving(unittest.TestCase):
    """交错场景：迟到补登记与固定旧修订回读交替进行。"""

    def test_interleaved_registration_and_pinned_evaluation(self):
        d = CheckpointDirectory()
        d.register(ck("A1", 1, params=[0]))
        d.register(ck("B1", 1, params=[4]))
        d.register(ck("A2", 2, parents=("A1",), params=[8]))
        rev_pinned = d.revision  # 固定此修订：此时 B2 与 M 尚未登记

        # 在固定修订下评估：步骤 3 无候选 -> 不可行
        m = manifest([1, 2, 3], window=3, alpha=Fraction(1, 2))
        with self.assertRaises(InfeasibleManifestError):
            evaluate(d, m, revision=rev_pinned)

        # 迟到补登记：B2（第 2 步）与合并点 M（第 3 步）
        d.register(ck("B2", 2, parents=("B1",), params=[2]))
        d.register(ck("M", 3, parents=("A2", "B2"), params=[3]))

        # 固定旧修订回读：结果与当时一致（仍不可行）
        with self.assertRaises(InfeasibleManifestError):
            evaluate(d, m, revision=rev_pinned)

        # 最新修订：可评估，结果与例 2 相同
        res = evaluate(d, m)
        self.assertEqual(res.dimensions[0].minimum, Fraction(3))
        self.assertFalse(res.publishable)

    def test_late_attempt_changes_current_but_not_pinned_result(self):
        """迟到补登一个同步新尝试：最新修订结果改变，固定旧修订不变。"""
        d = CheckpointDirectory()
        d.register(ck("a", 1, params=[6]))
        d.register(ck("c", 2, parents=("a",), params=[0]))
        rev_pinned = d.revision
        m = manifest([1, 2], window=2, alpha=Fraction(1, 2))

        res_before = evaluate(d, m, revision=rev_pinned)
        self.assertTrue(res_before.publishable)
        self.assertEqual(res_before.dimensions[0].minimum, Fraction(3))

        # 迟到补登记第 1 步的另一个尝试（值更小），它也有到第 2 步的链
        d.register(ck("b", 1, params=[0]))
        d.register(ck("e", 2, parents=("b",), params=[0]))

        res_pinned = evaluate(d, m, revision=rev_pinned)
        self.assertEqual(res_pinned, res_before)  # 旧修订回读完全可复现

        res_now = evaluate(d, m)
        self.assertFalse(res_now.publishable)  # 新尝试引入竞争
        self.assertEqual(res_now.dimensions[0].minimum, Fraction(0))
        self.assertEqual(res_now.dimensions[0].maximum, Fraction(3))
        self.assertEqual(res_now.divergence.step, 1)

    def test_revision_zero_sees_nothing(self):
        d = CheckpointDirectory()
        d.register(ck("a", 1, params=[1]))
        self.assertEqual(d.at_step(1, revision=0), [])
        self.assertIsNone(d.get("a", revision=0))


class TestWitnessPathCompleteness(unittest.TestCase):
    def test_witness_includes_intermediate_ancestors(self):
        """选中检查点之间隔着的中间祖先也要出现在完整见证路径里。"""
        d = CheckpointDirectory()
        d.register(ck("r", 0, params=[0]))
        d.register(ck("mid", 1, parents=("r",), params=[1]))
        d.register(ck("c2", 2, parents=("mid",), params=[2]))
        d.register(ck("c4", 4, parents=("c2",), params=[4]))  # 清单跳过第 3 步
        m = manifest([2, 4], window=2, alpha=Fraction(1, 2))
        res = evaluate(d, m)
        wit = res.dimensions[0].min_witness
        self.assertEqual(wit.chosen, ("c2", "c4"))
        # 完整路径包含根与中间祖先，且共享祖先只算一次
        self.assertEqual(wit.path, ("r", "mid", "c2", "c4"))

    def test_merge_path_dedup_shared_ancestors(self):
        """两支共享根祖先，合并后见证路径中根只出现一次。"""
        d = CheckpointDirectory()
        d.register(ck("root", 0, params=[0]))
        d.register(ck("a1", 1, parents=("root",), params=[1]))
        d.register(ck("b1", 1, parents=("root",), params=[2]))
        d.register(ck("m", 2, parents=("a1", "b1"), params=[3]))
        m = manifest([1, 2], window=2, alpha=Fraction(1, 2))
        res = evaluate(d, m)
        for dim in res.dimensions:
            for wit in (dim.min_witness, dim.max_witness):
                self.assertEqual(len(wit.path), len(set(wit.path)))
                self.assertEqual(wit.path[0], "root")
                self.assertEqual(wit.path[-1], "m")


if __name__ == "__main__":
    unittest.main()
