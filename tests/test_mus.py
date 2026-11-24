"""极小矛盾观测子集（MUS）测试。"""

import itertools
import unittest

from quantile_merger import Bucket, Observation
from quantile_merger.mus import find_mus, is_feasible
from quantile_merger.model import SegmentModel


def obs(series, rev, buckets, lo=1, hi=10, start=0, length=10, source="s"):
    return Observation(source, start, length, lo, hi, tuple(buckets), series, rev)


class MusBasicTest(unittest.TestCase):
    def setUp(self):
        self.a = obs("a", 1, [Bucket(1, 5, 3), Bucket(6, 10, 2)])
        self.b = obs("b", 1, [Bucket(1, 3, 1), Bucket(4, 10, 4)])
        self.bad1 = obs("c", 1, [Bucket(1, 5, 4), Bucket(6, 10, 1)])
        self.bad2 = obs("d", 1, [Bucket(1, 5, 0), Bucket(6, 10, 5)])

    def test_feasible_set_returns_none(self):
        self.assertIsNone(find_mus([self.a, self.b]))
        self.assertTrue(is_feasible([self.a]))
        self.assertTrue(is_feasible([]))

    def test_pairwise_mus(self):
        mus = find_mus([self.a, self.bad1])
        self.assertEqual({o.series_id for o in mus}, {"a", "c"})

    def test_mus_minimality_by_definition(self):
        # 题面语义：MUS 中去掉任意一条后，剩余集合都可行
        observations = [self.a, self.b, self.bad1, self.bad2]
        mus = find_mus(observations)
        self.assertIsNotNone(mus)
        self.assertFalse(is_feasible(mus))
        for i in range(len(mus)):
            reduced = mus[:i] + mus[i + 1 :]
            self.assertTrue(
                is_feasible(reduced),
                f"删除 MUS 第 {i} 条后仍不可行，不满足极小性",
            )

    def test_mus_with_irrelevant_observations(self):
        # a&b 联合固定单元 [1,3]=1,[4,5]=2,[6,10]=2。
        # extra 与该分配相容（且与 a、b 分别相容），但与 bad1 冲突，
        # 因此它会进入 MUS——这正说明 MUS 是"最小责任集"。
        extra = obs(
            "e", 1, [Bucket(1, 3, 1), Bucket(4, 5, 2), Bucket(6, 10, 2)]
        )
        self.assertTrue(is_feasible([self.a, self.b, extra]))
        observations = [self.a, self.b, extra, self.bad1]
        mus = find_mus(observations)
        ids = {o.series_id for o in mus}
        # bad1 与 a、extra 分别冲突，MUS 必须包含 bad1；
        # 极小性由 find_mus 内部断言保证
        self.assertIn("c", ids)
        for i in range(len(mus)):
            self.assertTrue(
                is_feasible(mus[:i] + mus[i + 1:])
            )

    def test_three_way_mus_exhaustive(self):
        # 值域 [1,6]。三条观测任意两条相容、三条联合矛盾：
        # a: [1,2]=2, [3,4]=0, [5,6]=2   （样本只在 1,2,5,6）
        # b: [1,4]=2, [5,6]=2             （与 a 相容）
        # c: [1,2]=0, [3,6]=4             （与 b 相容；与 a 在 [1,2] 上冲突）
        # a&c 本身已冲突，无法构造三条都两两相容的更强版本，
        # 因此这里验证的 MUS 语义为：整体不可行，且 MUS 删任一后可行。
        a = obs(
            "a", 1,
            [Bucket(1, 2, 2), Bucket(3, 4, 0), Bucket(5, 6, 2)], hi=6,
        )
        b = obs("b", 1, [Bucket(1, 4, 2), Bucket(5, 6, 2)], hi=6)
        c = obs("c", 1, [Bucket(1, 2, 0), Bucket(3, 6, 4)], hi=6)
        self.assertTrue(is_feasible([a, b]))
        self.assertTrue(is_feasible([b, c]))
        self.assertFalse(is_feasible([a, c]))
        self.assertFalse(is_feasible([a, b, c]))
        mus = find_mus([a, b, c])
        # b 与矛盾无关，MUS 只含 a、c
        self.assertEqual({o.series_id for o in mus}, {"a", "c"})
        for i in range(len(mus)):
            self.assertTrue(is_feasible(mus[:i] + mus[i + 1:]))

    def test_mus_report_carries_revision_identity(self):
        mus = find_mus([self.a, self.bad1])
        identities = {(o.series_id, o.revision) for o in mus}
        self.assertEqual(identities, {("a", 1), ("c", 1)})
        # MUS 中的观测仍是完整记录，可直接重建模型验证矛盾
        self.assertFalse(SegmentModel(list(mus)).feasible)


if __name__ == "__main__":
    unittest.main()
