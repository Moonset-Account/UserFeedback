"""pcm_stitch 验收测试。

覆盖：
  * 可接受结果：唯一合法接缝、锚点成立、确认区段可反查来源；
  * 应被拒绝的结果：接缝样本不全等、锚点互相矛盾、首段锚点冲突；
  * 边界/交错场景：周期片段多解歧义、锚点消歧、零重叠、
    重叠范围上界截断、三段链式传播、非法输入校验。
"""

import unittest

from pcm_stitch import Anchor, Seam, stitch


class TestAcceptableStitch(unittest.TestCase):
    """可接受结果：唯一接缝 + 锚点同时成立，全程可反查。"""

    def test_unique_seam_fully_confirmed(self):
        seg0 = [1, 2, 3, 4, 5]
        seg1 = [4, 5, 6, 7]
        # 范围 [1,3] 内仅 k=2 满足 seg0 末尾 == seg1 开头。
        result = stitch([seg0, seg1], [(1, 3)], anchors=[Anchor(1, 2, 5)])
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.solutions, ((2,),))
        self.assertEqual(result.total_length, 7)
        # 合并后应为 [1,2,3,4,5,6,7]，全部已确认。
        self.assertEqual(result.ambiguous_regions, ())
        self.assertEqual(
            [result.value_at(p) for p in range(7)],
            [1, 2, 3, 4, 5, 6, 7],
        )
        # 每个已确认样本都能反查到 (片段, 片段内偏移)。
        expected_sources = [
            (0, 0), (0, 1), (0, 2), (0, 3), (0, 4), (1, 2), (1, 3),
        ]
        for p, want in enumerate(expected_sources):
            self.assertEqual(result.source_at(p), want, f"position {p}")
        # 锚点确实成立：片段 1 偏移 2 -> 绝对位置 5。
        self.assertEqual(result.source_at(5), (1, 2))

    def test_zero_overlap_boundary(self):
        # lo=0 边界：允许完全不重叠的拼接。
        result = stitch([[1, 2], [3, 4]], [(0, 1)])
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.solutions, ((0,),))
        self.assertEqual([result.value_at(p) for p in range(4)], [1, 2, 3, 4])
        self.assertEqual(result.source_at(3), (1, 1))

    def test_overlap_range_clamped_to_segment_length(self):
        # hi 远超片段长度，应被截断；k=2 为唯一可行重叠。
        result = stitch([[1, 2], [1, 2, 9]], [Seam(2, 99)])
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.solutions, ((2,),))
        self.assertEqual(result.total_length, 3)
        self.assertEqual(result.source_at(2), (1, 2))
        self.assertEqual(result.value_at(2), 9)

    def test_three_segments_chained(self):
        # 三段交错：第二个接缝的可行重叠依赖第一个接缝的取值。
        seg0 = [1, 2, 3]
        seg1 = [2, 3, 2, 3]
        seg2 = [3, 2, 9]
        result = stitch(
            [seg0, seg1, seg2],
            [(1, 2), (1, 2)],
            anchors=[Anchor(2, 2, 6)],
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.solutions, ((2, 1),))
        merged = [result.value_at(p) for p in range(result.total_length)]
        self.assertEqual(merged, [1, 2, 3, 2, 3, 2, 9])
        # 全程反查：来源片段的样本与输出值一致。
        for p in range(result.total_length):
            src = result.source_at(p)
            self.assertIsNotNone(src)
            seg, off = src
            self.assertEqual(result.segments[seg][off], result.value_at(p))


class TestRejectedStitch(unittest.TestCase):
    """应被阻止或拒绝的结果：无合法拼法时保留原段。"""

    def test_mismatched_seam_rejected(self):
        seg0 = [1, 2, 3]
        seg1 = [9, 9, 9]
        result = stitch([seg0, seg1], [(1, 2)])
        self.assertEqual(result.status, "no_feasible")
        self.assertEqual(result.solutions, ())
        self.assertEqual(result.regions, ())
        # 保留原段。
        self.assertEqual(result.segments, (tuple(seg0), tuple(seg1)))

    def test_conflicting_anchors_rejected(self):
        # 接缝唯一 (k=2 -> 片段1起点为2)，但锚点要求片段1起点为0。
        result = stitch(
            [[1, 2, 3, 4], [3, 4, 5]],
            [(1, 2)],
            anchors=[Anchor(0, 0, 0), Anchor(1, 0, 0)],
        )
        self.assertEqual(result.status, "no_feasible")
        self.assertEqual(result.segments, ((1, 2, 3, 4), (3, 4, 5)))

    def test_first_segment_anchor_conflict(self):
        # 首段从零开始：锚点 (0, 2, 5) 不可能成立。
        result = stitch([[1, 2, 3], [3, 4]], [(1, 1)], anchors=[Anchor(0, 2, 5)])
        self.assertEqual(result.status, "no_feasible")

    def test_anchor_must_hold_in_all_solutions(self):
        # 周期片段本有多解；锚点与所有可行接缝都冲突 -> 拒绝。
        result = stitch(
            [[7, 7, 7, 7], [7, 7, 7]],
            [(1, 3)],
            anchors=[Anchor(1, 0, 0)],  # 要求片段1起点为0，但可行起点为 1..3
        )
        self.assertEqual(result.status, "no_feasible")


class TestAmbiguity(unittest.TestCase):
    """边界/交错场景：周期片段多解 -> 歧义标注与锚点消歧。"""

    def test_periodic_segments_mark_ambiguous(self):
        # 全同值片段：k=1,2,3 均看似合适，产生三种拼法。
        result = stitch([[7, 7, 7, 7], [7, 7, 7]], [(1, 3)])
        self.assertEqual(result.status, "ok")
        self.assertEqual(sorted(result.solutions), [(1,), (2,), (3,)])
        # 三种拼法长度分别为 6/5/4，时间轴覆盖 [0,6)。
        self.assertEqual(result.total_length, 6)
        # 仅 [0,4) 的来源映射被全部拼法支持。
        confirmed = result.confirmed_regions
        self.assertEqual(len(confirmed), 1)
        self.assertEqual((confirmed[0].start, confirmed[0].end), (0, 4))
        self.assertEqual((confirmed[0].segment, confirmed[0].offset), (0, 0))
        # [4,6) 为歧义区段。
        ambiguous = result.ambiguous_regions
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual((ambiguous[0].start, ambiguous[0].end), (4, 6))
        # 歧义位置不可反查来源。
        self.assertIsNone(result.source_at(4))
        self.assertIsNone(result.source_at(5))
        # 但所有覆盖该位置的拼法样本值一致，仍可给出取值。
        self.assertEqual(result.value_at(4), 7)
        self.assertEqual(result.value_at(5), 7)
        # 确认位置反查正常。
        self.assertEqual(result.source_at(3), (0, 3))

    def test_anchor_disambiguates(self):
        # 同样的周期片段，锚点 (1,0)->2 锁定 k=2，歧义消除。
        result = stitch(
            [[7, 7, 7, 7], [7, 7, 7]],
            [(1, 3)],
            anchors=[Anchor(1, 0, 2)],
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.solutions, ((2,),))
        self.assertEqual(result.ambiguous_regions, ())
        self.assertEqual(result.total_length, 5)
        self.assertEqual(result.source_at(4), (1, 2))
        self.assertEqual(result.source_at(3), (0, 3))

    def test_value_conflict_in_ambiguous_region(self):
        # 歧义区段中样本值也可能不一致：value_at 返回 None。
        # seg0 尾部与 seg1 头部在 k=1 与 k=2 都匹配，但 seg1 后续样本不同。
        seg0 = [0, 5, 5]
        seg1 = [5, 5, 8]
        # k=1: [5]==[5]；k=2: [5,5]==[5,5]。两种拼法长度为 5 和 4。
        result = stitch([seg0, seg1], [(1, 2)])
        self.assertEqual(result.status, "ok")
        self.assertEqual(sorted(result.solutions), [(1,), (2,)])
        # 位置 4 仅被 k=1 拼法覆盖（值为 8），k=2 拼法不覆盖 -> 歧义。
        self.assertIsNone(result.source_at(4))
        self.assertEqual(result.value_at(4), 8)  # 覆盖它的拼法取值一致
        # 位置 3 在两种拼法中分别来自 (1,1) 与 (1,2)，来源不同 -> 歧义。
        self.assertIsNone(result.source_at(3))


class TestInputValidation(unittest.TestCase):
    def test_seam_count_mismatch(self):
        with self.assertRaises(ValueError):
            stitch([[1], [2], [3]], [(0, 0)])

    def test_invalid_seam_range(self):
        with self.assertRaises(ValueError):
            stitch([[1], [1]], [(2, 1)])
        with self.assertRaises(ValueError):
            stitch([[1], [1]], [(-1, 1)])

    def test_anchor_out_of_range(self):
        with self.assertRaises(ValueError):
            stitch([[1], [1]], [(0, 1)], anchors=[Anchor(2, 0, 0)])
        with self.assertRaises(ValueError):
            stitch([[1], [1]], [(0, 1)], anchors=[Anchor(0, 5, 0)])
        with self.assertRaises(ValueError):
            stitch([[1], [1]], [(0, 1)], anchors=[Anchor(0, 0, -1)])

    def test_empty_segment_rejected(self):
        with self.assertRaises(ValueError):
            stitch([], [])
        with self.assertRaises(ValueError):
            stitch([[1], []], [(0, 0)])

    def test_non_integer_sample_rejected(self):
        with self.assertRaises(TypeError):
            stitch([[1, "x"], [1]], [(0, 1)])

    def test_single_segment(self):
        result = stitch([[4, 5, 6]], [])
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.solutions, ((),))
        self.assertEqual(result.total_length, 3)
        self.assertEqual(result.source_at(1), (0, 1))


if __name__ == "__main__":
    unittest.main()
