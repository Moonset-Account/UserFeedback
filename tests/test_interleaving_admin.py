"""两个会话交错取号 + 管理变更（改步长 / 指定下一值）的精确测试。

覆盖题面关键要求：
- 每会话先预留小批号码再逐次发放；
- 两类管理变更都收回尚未消费的会话号段；
- 改步长从旧版本已预留到的边界沿新步长继续；
- restart_with 后下一个发放值精确等于指定值；
- 反向序列同样适用。
"""

import unittest

from sequence_allocator import (
    SequenceCatalog,
    SequenceConfigError,
    SequenceExhaustedError,
)


class TestInterleavingAdminChanges(unittest.TestCase):
    def setUp(self):
        self.catalog = SequenceCatalog()
        # 正向，步长 1，每会话预留 3 个，上限 200。
        self.catalog.create_sequence(
            "order_id", start_value=1, increment_by=1,
            min_value=1, max_value=200, cache_size=3, cycle=False,
        )
        self.s1 = self.catalog.open_session("order_id")
        self.s2 = self.catalog.open_session("order_id")

    def tearDown(self):
        self.s1.close()
        self.s2.close()

    def test_interleaved_batches_before_change(self):
        self.assertEqual(self.s1.nextval(), 1)   # A 预留 1..3
        self.assertEqual(self.s2.nextval(), 4)   # B 预留 4..6
        self.assertEqual(self.s1.nextval(), 2)
        self.assertEqual(self.s2.nextval(), 5)
        self.assertEqual(self.catalog.get_state("order_id").watermark, 6)

    def test_alter_increment_reclaims_and_continues_from_boundary(self):
        self.assertEqual(self.s1.nextval(), 1)
        self.assertEqual(self.s2.nextval(), 4)
        self.assertEqual(self.s1.nextval(), 2)
        self.assertEqual(self.s2.nextval(), 5)

        self.catalog.alter_increment("order_id", 5)
        state = self.catalog.get_state("order_id")
        # watermark 保持旧边界 6；版本 +1；两个号段都被收回。
        self.assertEqual(state.watermark, 6)
        self.assertEqual(state.version, 2)
        self.assertEqual(state.reclaimed_batches, 2)
        # 会话缓存已被主动清空。
        self.assertEqual(self.s1.cached_count, 0)
        self.assertEqual(self.s2.cached_count, 0)

        # 从边界 6 沿新步长 +5 继续：A 预留 11,16,21；B 预留 26,31,36。
        self.assertEqual(self.s1.nextval(), 11)
        self.assertEqual(self.s2.nextval(), 26)
        self.assertEqual(self.s1.nextval(), 16)
        self.assertEqual(self.s2.nextval(), 31)
        self.assertEqual(self.s1.nextval(), 21)
        self.assertEqual(self.s2.nextval(), 36)
        # 7..10 永久空缺。
        self.assertEqual(self.catalog.get_state("order_id").watermark, 36)

    def test_reclaimed_values_are_never_redistributed(self):
        # A 预留 1..3，只用掉 1；B 预留 4..6，只用掉 4。
        self.assertEqual(self.s1.nextval(), 1)
        self.assertEqual(self.s2.nextval(), 4)
        self.catalog.alter_increment("order_id", 2)
        # 收回 2,3,5,6；watermark=6，下一个值 8。
        first_after = self.s1.nextval()
        self.assertEqual(first_after, 8)
        issued = [1, 4, 8]
        for gap in (2, 3, 5, 6, 7):
            self.assertNotIn(gap, issued)

    def test_restart_with_sets_exact_next_value(self):
        self.assertEqual(self.s1.nextval(), 1)
        self.assertEqual(self.s2.nextval(), 4)
        self.catalog.restart_with("order_id", 100)
        state = self.catalog.get_state("order_id")
        self.assertEqual(state.version, 2)
        self.assertEqual(state.reclaimed_batches, 2)
        self.assertEqual(self.s1.nextval(), 100)  # s1 新预留 100,101,102
        self.assertEqual(self.s2.nextval(), 103)  # s2 新预留 103,104,105
        self.assertEqual(self.s1.nextval(), 101)

    def test_two_admin_changes_bump_version_twice(self):
        self.s1.nextval()
        self.s2.nextval()
        self.catalog.alter_increment("order_id", 3)
        self.assertEqual(self.catalog.get_state("order_id").version, 2)
        self.catalog.restart_with("order_id", 50)
        state = self.catalog.get_state("order_id")
        self.assertEqual(state.version, 3)
        self.assertEqual(state.watermark, 47)  # 50 - 3
        self.assertEqual(self.s1.nextval(), 50)

    def test_alter_increment_boundary_on_descending_sequence(self):
        catalog = SequenceCatalog()
        catalog.create_sequence(
            "down", start_value=10, increment_by=-2,
            min_value=0, max_value=10, cache_size=3, cycle=False,
        )
        c = catalog.open_session("down")
        d = catalog.open_session("down")
        self.assertEqual(c.nextval(), 10)  # C: 10,8,6
        self.assertEqual(d.nextval(), 4)   # D: 4,2,0
        self.assertEqual(d.nextval(), 2)

        catalog.alter_increment("down", -3)
        state = catalog.get_state("down")
        self.assertEqual(state.watermark, 0)  # 旧预留边界
        self.assertEqual(state.reclaimed_batches, 2)
        with self.assertRaises(SequenceExhaustedError):
            c.nextval()  # 0 + (-3) < min_value，立即结束
        c.close()
        d.close()

    def test_alter_to_descending_then_restart(self):
        catalog = SequenceCatalog()
        catalog.create_sequence(
            "flip", start_value=1, increment_by=1,
            min_value=-100, max_value=100, cache_size=4, cycle=False,
        )
        a = catalog.open_session("flip")
        b = catalog.open_session("flip")
        self.assertEqual([a.nextval() for _ in range(4)], [1, 2, 3, 4])
        self.assertEqual([b.nextval() for _ in range(4)], [5, 6, 7, 8])
        # 正向翻反向：从边界 8 沿 -2 继续，A 预留 6,4,2,0；B 预留 -2,-4,-6,-8。
        catalog.alter_increment("flip", -2)
        self.assertEqual(a.nextval(), 6)
        self.assertEqual(b.nextval(), -2)
        catalog.restart_with("flip", 80)
        # restart 后 A 先预留 80,78,76,74，B 再预留 72,70,68,66。
        self.assertEqual(a.nextval(), 80)
        self.assertEqual(b.nextval(), 72)
        a.close()
        b.close()

    def test_invalid_admin_arguments(self):
        with self.assertRaises(SequenceConfigError):
            self.catalog.alter_increment("order_id", 0)
        with self.assertRaises(SequenceConfigError):
            self.catalog.restart_with("order_id", 999)  # 超过 max_value
        with self.assertRaises(SequenceConfigError):
            self.catalog.restart_with("order_id", 0)    # 低于 min_value

    def test_stale_session_self_heals_even_without_active_invalidation(self):
        # 直接在条目层制造版本推进，验证会话的惰性失效兜底。
        self.s1.nextval()  # s1 预留 1..3，s2 尚未预留，watermark=3
        entry = self.s1._entry  # noqa: SLF001
        with entry.lock:
            entry.alter_increment(2)
        # 目录没有主动失效会话，nextval 也必须感知版本变化并重新预留。
        self.assertEqual(self.s1.version, 1)
        value = self.s1.nextval()  # 旧边界 3 沿 +2：首批 5,7,9
        self.assertEqual(self.s1.version, 2)
        self.assertEqual(value, 5)
        self.assertEqual(self.s1.nextval(), 7)


if __name__ == "__main__":
    unittest.main()
