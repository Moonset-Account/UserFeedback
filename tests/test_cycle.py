"""绕回（cycle）语义测试：绕回前必须等当前号段用完或释放。"""

import unittest

from sequence_allocator import SequenceCatalog, SequenceExhaustedError


class TestCycle(unittest.TestCase):
    def test_wrap_after_all_ranges_consumed_single_session(self):
        catalog = SequenceCatalog()
        catalog.create_sequence(
            "c", start_value=1, increment_by=1,
            min_value=1, max_value=5, cache_size=2, cycle=True,
        )
        session = catalog.open_session("c")
        self.assertEqual([session.nextval() for _ in range(5)], [1, 2, 3, 4, 5])
        # 5 是短批最后一个，号段已释放，下一次顺利绕回。
        self.assertEqual(session.nextval(), 1)
        self.assertEqual(session.nextval(), 2)
        session.close()

    def test_wrap_waits_while_other_session_holds_range(self):
        catalog = SequenceCatalog()
        catalog.create_sequence(
            "c", start_value=1, increment_by=1,
            min_value=1, max_value=6, cache_size=3, cycle=True,
        )
        a = catalog.open_session("c")
        b = catalog.open_session("c")
        self.assertEqual(a.nextval(), 1)  # A 持有 1..3
        self.assertEqual(b.nextval(), 4)  # B 预留 4..6
        self.assertEqual(b.nextval(), 5)
        self.assertEqual(b.nextval(), 6)  # B 缓存耗尽，[4,6] 释放
        self.assertEqual(a.nextval(), 2)  # A 仍发缓存；B 抢走短批 [6]，watermark 到界
        with self.assertRaises(SequenceExhaustedError):
            b.nextval()  # A 仍持有 [1,3]，B 想绕回必须等待
        self.assertEqual(a.nextval(), 3)  # A 用完 [1,3] 并释放，绕回通道打开
        self.assertEqual(a.nextval(), 1)  # A 绕回，新批 1..3
        # B 此时成功预留 4..6（绕回后的第二段），不再等待。
        self.assertEqual(b.nextval(), 4)
        self.assertEqual(a.nextval(), 2)
        self.assertEqual(a.nextval(), 3)  # A 释放 [1,3]
        with self.assertRaises(SequenceExhaustedError):
            a.nextval()  # B 仍持有 [4,6]，轮到 A 等待
        self.assertEqual(b.nextval(), 5)
        self.assertEqual(b.nextval(), 6)  # B 用完释放
        self.assertEqual(a.nextval(), 1)  # 无号段持有，A 再次绕回
        a.close()
        b.close()

    def test_wrap_after_explicit_close_release(self):
        catalog = SequenceCatalog()
        catalog.create_sequence(
            "c", start_value=1, increment_by=1,
            min_value=1, max_value=4, cache_size=3, cycle=True,
        )
        a = catalog.open_session("c")
        b = catalog.open_session("c")
        self.assertEqual(a.nextval(), 1)  # A: 1,2,3
        self.assertEqual(b.nextval(), 4)  # B 短批 [4]，到界
        b.close()  # 释放 [4]
        self.assertEqual(a.nextval(), 2)
        self.assertEqual(a.nextval(), 3)
        self.assertEqual(a.nextval(), 1)  # 无号段持有，绕回
        a.close()

    def test_descending_cycle_wrap(self):
        catalog = SequenceCatalog()
        catalog.create_sequence(
            "c", start_value=3, increment_by=-1,
            min_value=1, max_value=3, cache_size=2, cycle=True,
        )
        session = catalog.open_session("c")
        self.assertEqual([session.nextval() for _ in range(3)], [3, 2, 1])
        self.assertEqual(session.nextval(), 3)  # 反向绕回起点
        session.close()

    def test_no_cycle_raises_at_boundary(self):
        catalog = SequenceCatalog()
        catalog.create_sequence(
            "c", start_value=4, increment_by=1,
            min_value=1, max_value=5, cache_size=3, cycle=False,
        )
        session = catalog.open_session("c")
        self.assertEqual([session.nextval(), session.nextval()], [4, 5])
        with self.assertRaises(SequenceExhaustedError):
            session.nextval()
        session.close()


if __name__ == "__main__":
    unittest.main()
