"""byte_capacity_cache 的单元测试。

从工作区根目录运行：
    python3 -m unittest discover -s tests -v
"""

import unittest

from byte_capacity_cache import AccessResult, ByteCapacityCache


class TestBasicInsertAndHit(unittest.TestCase):
    """正常场景：直接进入、命中更新、最近端移动。"""

    def test_insert_when_space_available(self):
        c = ByteCapacityCache(capacity=10, window=100)
        r1 = c.access("a", 4, "va")
        r2 = c.access("b", 4, "vb")
        self.assertTrue(r1.admitted)
        self.assertFalse(r1.hit)
        self.assertEqual(r1.evicted_keys, [])
        self.assertEqual(r2.cache_bytes, 8)
        self.assertEqual(c.keys(), ["a", "b"])

    def test_hit_updates_value_and_moves_to_mru(self):
        c = ByteCapacityCache(capacity=10, window=100)
        c.access("a", 4, "v1")
        c.access("b", 4, "v2")
        r = c.access("a", 4, "v3")
        self.assertTrue(r.hit)
        self.assertTrue(r.admitted)
        self.assertEqual(c.get_value("a"), "v3")
        # a 移到最近端，b 成为最久未用。
        self.assertEqual(c.keys(), ["b", "a"])
        self.assertEqual(r.cache_bytes, 8)

    def test_frequency_increments_on_every_access(self):
        c = ByteCapacityCache(capacity=10, window=100)
        c.access("a", 4)
        c.access("a", 4)
        c.access("b", 4)
        self.assertEqual(c.frequency("a"), 2)
        self.assertEqual(c.frequency("b"), 1)
        self.assertEqual(c.frequency("never-seen"), 0)


class TestFrequencyGateEviction(unittest.TestCase):
    """未命中淘汰：严格大于拟淘汰项频率之和才替换。"""

    def test_reject_when_candidate_freq_not_strictly_greater(self):
        c = ByteCapacityCache(capacity=10, window=100)
        c.access("a", 4)  # freq a = 1
        c.access("a", 4)  # freq a = 2
        c.access("b", 4)  # freq b = 1，次序 [a, b]
        # c 需要 4 字节：缺 2，最久未用前缀为 [a]，频率和 = 2。
        r1 = c.access("c", 4)  # freq c = 1，1 > 2 不成立，拒绝
        self.assertFalse(r1.admitted)
        self.assertEqual(r1.evicted_keys, [])
        self.assertEqual(r1.cache_bytes, 8)
        r2 = c.access("c", 4)  # freq c = 2，2 > 2 不成立（必须严格大于），拒绝
        self.assertFalse(r2.admitted)
        r3 = c.access("c", 4)  # freq c = 3 > 2，替换
        self.assertTrue(r3.admitted)
        self.assertEqual(r3.evicted_keys, ["a"])
        self.assertEqual(r3.cache_bytes, 8)
        self.assertEqual(c.keys(), ["b", "c"])

    def test_rejection_preserves_cache_order(self):
        c = ByteCapacityCache(capacity=10, window=100)
        c.access("a", 4)
        c.access("b", 4)
        before = c.keys()
        c.access("c", 4)  # freq c = 1，不大于 a 的频率，拒绝
        self.assertEqual(c.keys(), before)
        self.assertNotIn("c", c)

    def test_sum_of_multiple_evict_candidate_frequencies(self):
        c = ByteCapacityCache(capacity=10, window=100)
        for k in ("a", "b", "c", "d"):
            c.access(k, 2)  # 每个键 freq = 1，bytes = 8
        # X 需要 6 字节：缺 4，最久未用前缀为 [a, b]，频率和 = 2。
        r1 = c.access("X", 6)  # freq X = 1，拒绝
        self.assertFalse(r1.admitted)
        r2 = c.access("X", 6)  # freq X = 2，2 > 2 不成立，拒绝
        self.assertFalse(r2.admitted)
        r3 = c.access("X", 6)  # freq X = 3 > 2，淘汰 a、b
        self.assertTrue(r3.admitted)
        self.assertEqual(r3.evicted_keys, ["a", "b"])
        self.assertEqual(r3.cache_bytes, 10)
        self.assertEqual(c.keys(), ["c", "d", "X"])

    def test_rejected_access_still_accumulates_frequency(self):
        c = ByteCapacityCache(capacity=10, window=100)
        c.access("a", 8)
        c.access("a", 8)  # freq a = 2
        c.access("big", 8)  # freq big = 1，1 > 2 不成立，拒绝
        c.access("big", 8)  # freq big = 2，2 > 2 不成立，拒绝
        self.assertEqual(c.frequency("big"), 2)
        self.assertNotIn("big", c)


class TestBoundary(unittest.TestCase):
    """边界场景：超容量、恰等于容量、零容量、零大小、W=1。"""

    def test_oversized_item_rejected_directly(self):
        c = ByteCapacityCache(capacity=10, window=100)
        c.access("a", 4)
        r = c.access("huge", 11)
        self.assertFalse(r.admitted)
        self.assertFalse(r.hit)
        self.assertEqual(r.evicted_keys, [])
        self.assertEqual(r.cache_bytes, 4)
        self.assertEqual(c.frequency("huge"), 1)  # 被拒也积累频率

    def test_item_exactly_capacity_evicts_everything_when_gated(self):
        c = ByteCapacityCache(capacity=8, window=100)
        c.access("a", 4)
        c.access("b", 4)  # bytes = 8，a、b freq 各 1
        # full 恰等于容量：需腾空全部 8 字节，频率和 = 2。
        r1 = c.access("full", 8)  # freq 1，拒绝
        self.assertFalse(r1.admitted)
        r2 = c.access("full", 8)  # freq 2，不严格大于，拒绝
        self.assertFalse(r2.admitted)
        r3 = c.access("full", 8)  # freq 3 > 2，全部淘汰
        self.assertTrue(r3.admitted)
        self.assertEqual(r3.evicted_keys, ["a", "b"])
        self.assertEqual(r3.cache_bytes, 8)

    def test_exact_capacity_item_enters_empty_cache_directly(self):
        c = ByteCapacityCache(capacity=8, window=100)
        r = c.access("full", 8)
        self.assertTrue(r.admitted)
        self.assertEqual(r.cache_bytes, 8)

    def test_zero_capacity(self):
        c = ByteCapacityCache(capacity=0, window=100)
        r = c.access("a", 1)
        self.assertFalse(r.admitted)  # 超容量直接拒绝
        r0 = c.access("z", 0)
        self.assertTrue(r0.admitted)  # 0 字节项可直接进入
        self.assertEqual(c.cache_bytes, 0)

    def test_zero_size_item(self):
        c = ByteCapacityCache(capacity=4, window=100)
        c.access("a", 4)
        r = c.access("z", 0)  # 不缺空间，直接进入
        self.assertTrue(r.admitted)
        self.assertEqual(c.keys(), ["a", "z"])

    def test_invalid_arguments(self):
        with self.assertRaises(ValueError):
            ByteCapacityCache(capacity=-1, window=10)
        with self.assertRaises(ValueError):
            ByteCapacityCache(capacity=10, window=0)
        c = ByteCapacityCache(capacity=10, window=10)
        with self.assertRaises(ValueError):
            c.access("a", -1)
        c.access("a", 4)
        with self.assertRaises(ValueError):
            c.access("a", 5)  # 同一键大小固定，矛盾输入报错


class TestHalving(unittest.TestCase):
    """每满 W 次访问，所有键频率向下折半。"""

    def test_halving_triggers_every_w_accesses(self):
        c = ByteCapacityCache(capacity=100, window=4)
        c.access("a", 1)  # 1
        c.access("a", 1)  # 2
        c.access("a", 1)  # 3
        r4 = c.access("b", 1)  # 4 -> 结算后折半
        self.assertTrue(r4.halved)
        self.assertEqual(c.frequency("a"), 1)   # 3 // 2
        self.assertEqual(c.frequency("b"), 0)   # 1 // 2，向下取整
        r5 = c.access("c", 1)
        self.assertFalse(r5.halved)
        r8 = None
        for _ in range(3):
            r8 = c.access("c", 1)
        self.assertTrue(r8.halved)  # 第 8 次再次折半

    def test_halving_applies_to_evicted_and_rejected_keys(self):
        c = ByteCapacityCache(capacity=10, window=2)
        c.access("a", 8)          # 1: a freq 1
        c.access("big", 8)        # 2: 被拒，freq big 1 -> 折半 a=0, big=0
        self.assertEqual(c.frequency("big"), 0)
        c.access("a", 8)          # 3: 命中，a freq 1
        c.access("big", 8)        # 4: big freq 1，1 > 1 不成立，拒绝 -> 折半
        self.assertEqual(c.frequency("a"), 0)
        self.assertEqual(c.frequency("big"), 0)

    def test_window_one_halves_after_every_access(self):
        c = ByteCapacityCache(capacity=10, window=1)
        c.access("a", 4)  # freq 1 -> 0
        c.access("a", 4)  # freq 1 -> 0
        self.assertEqual(c.frequency("a"), 0)


class TestBigObjectAcrossHalvingBoundary(unittest.TestCase):
    """交错场景：同一大对象连续尝试跨过折半边界，逐步复算每次决策。"""

    def test_decisions_recomputable_from_results(self):
        c = ByteCapacityCache(capacity=10, window=5)
        results = []
        results.append(c.access("a", 5))   # 1: 进入，bytes 5
        results.append(c.access("b", 5))   # 2: 进入，bytes 10
        results.append(c.access("X", 8))   # 3: freq X=1，需淘汰 [a,b] 和为 2，拒绝
        results.append(c.access("X", 8))   # 4: freq X=2，不严格大于 2，拒绝
        results.append(c.access("X", 8))   # 5: freq X=3 > 2，淘汰 a、b；随后折半
        results.append(c.access("a", 5))   # 6: a freq 0+1=1，需淘汰 [X] 和为 1，拒绝
        results.append(c.access("a", 5))   # 7: a freq 2 > 1，淘汰 X

        # 逐步核对返回的命中、淘汰键与缓存字节数。
        self.assertEqual(
            [(r.hit, r.admitted, r.evicted_keys, r.cache_bytes) for r in results],
            [
                (False, True, [], 5),
                (False, True, [], 10),
                (False, False, [], 10),
                (False, False, [], 10),
                (False, True, ["a", "b"], 8),
                (False, False, [], 8),
                (False, True, ["X"], 5),
            ],
        )
        # 折半只发生在第 5 次访问结算后。
        self.assertEqual([r.halved for r in results],
                         [False, False, False, False, True, False, False])
        # 折半后频率：X = 3//2 = 1，a/b = 1//2 = 0（a 在第 6 次重新累积）。
        self.assertEqual(results[4].key_frequency, 3)   # 折半前记录
        self.assertEqual(results[5].key_frequency, 1)   # 0 + 1
        self.assertEqual(results[6].key_frequency, 2)
        # X 第 5 次折半为 1，第 7 次被淘汰后频率仍保留在频率表中。
        self.assertEqual(c.frequency("X"), 1)
        self.assertEqual(c.frequency("a"), 2)
        self.assertEqual(c.keys(), ["a"])
        self.assertEqual(c.access_count, 7)

    def test_recompute_decision_from_returned_fields(self):
        """仅使用 AccessResult 携带的信息即可复算淘汰决策。"""
        c = ByteCapacityCache(capacity=10, window=1000)
        c.access("a", 5)
        c.access("b", 5)
        # 复算器：根据返回的淘汰键与字节数验证空间守恒。
        prev_bytes = c.cache_bytes  # 已有 a、b，共 10 字节
        for _ in range(3):
            r = c.access("X", 8)
            freed = 5 * len(r.evicted_keys)  # a、b 各 5 字节
            if r.admitted:
                self.assertEqual(r.cache_bytes, prev_bytes - freed + 8)
            else:
                self.assertEqual(r.cache_bytes, prev_bytes)
                self.assertEqual(r.evicted_keys, [])
            prev_bytes = r.cache_bytes
        # 第三次 freq X = 3 > (1+1)，成功进入。
        self.assertTrue(r.admitted)
        self.assertEqual(r.cache_bytes, 8)


class TestAccessResultIntegrity(unittest.TestCase):
    """返回结果字段完整性。"""

    def test_result_fields(self):
        c = ByteCapacityCache(capacity=6, window=2)
        r1 = c.access("a", 6, "va")
        self.assertIsInstance(r1, AccessResult)
        self.assertEqual(r1.access_index, 1)
        self.assertEqual(r1.key, "a")
        self.assertEqual(r1.key_frequency, 1)
        r2 = c.access("a", 6, "vb")
        self.assertEqual(r2.access_index, 2)
        self.assertTrue(r2.hit)
        self.assertTrue(r2.halved)
        self.assertEqual(c.get_value("a"), "vb")


if __name__ == "__main__":
    unittest.main()
