"""正向/反向序列的基本发放、短批、到界结束等正常与边界场景。"""

import unittest

from sequence_allocator import (
    SequenceCatalog,
    SequenceExhaustedError,
    SequenceSessionClosedError,
)


class TestBasicAllocation(unittest.TestCase):
    def test_ascending_single_session_batches(self):
        catalog = SequenceCatalog()
        catalog.create_sequence("s", start_value=1, increment_by=1,
                                min_value=1, max_value=100, cache_size=3)
        session = catalog.open_session("s")
        # 首批 1..3
        self.assertEqual([session.nextval() for _ in range(3)], [1, 2, 3])
        # 第二批 4..6
        self.assertEqual([session.nextval() for _ in range(3)], [4, 5, 6])
        self.assertEqual(catalog.get_state("s").watermark, 6)
        session.close()

    def test_custom_increment_ascending(self):
        catalog = SequenceCatalog()
        catalog.create_sequence("s", start_value=10, increment_by=5,
                                min_value=1, max_value=100, cache_size=4)
        session = catalog.open_session("s")
        self.assertEqual([session.nextval() for _ in range(4)], [10, 15, 20, 25])
        self.assertEqual(catalog.get_state("s").watermark, 25)

    def test_descending_sequence(self):
        catalog = SequenceCatalog()
        catalog.create_sequence("s", start_value=20, increment_by=-4,
                                min_value=0, max_value=20, cache_size=3)
        session = catalog.open_session("s")
        # 首批 20,16,12（watermark=12），第二批 8,4,0（watermark=0）。
        self.assertEqual([session.nextval() for _ in range(4)], [20, 16, 12, 8])
        self.assertEqual(catalog.get_state("s").watermark, 0)
        session.close()

    def test_short_batch_at_max_value_no_cycle(self):
        catalog = SequenceCatalog()
        catalog.create_sequence("s", start_value=9, increment_by=1,
                                min_value=1, max_value=10, cache_size=5, cycle=False)
        session = catalog.open_session("s")
        # 只能取到 9,10 的短批
        self.assertEqual([session.nextval(), session.nextval()], [9, 10])
        with self.assertRaises(SequenceExhaustedError):
            session.nextval()

    def test_descending_short_batch_at_min_value(self):
        catalog = SequenceCatalog()
        catalog.create_sequence("s", start_value=3, increment_by=-2,
                                min_value=0, max_value=10, cache_size=5, cycle=False)
        session = catalog.open_session("s")
        self.assertEqual([session.nextval(), session.nextval()], [3, 1])
        with self.assertRaises(SequenceExhaustedError):
            session.nextval()

    def test_exhaustion_is_sticky(self):
        catalog = SequenceCatalog()
        catalog.create_sequence("s", start_value=10, increment_by=1,
                                min_value=1, max_value=10, cache_size=3, cycle=False)
        session = catalog.open_session("s")
        self.assertEqual(session.nextval(), 10)
        with self.assertRaises(SequenceExhaustedError):
            session.nextval()
        with self.assertRaises(SequenceExhaustedError):
            session.nextval()  # 再次尝试仍然结束

    def test_closed_session_rejects_nextval(self):
        catalog = SequenceCatalog()
        catalog.create_sequence("s", cache_size=2)
        session = catalog.open_session("s")
        session.close()
        with self.assertRaises(SequenceSessionClosedError):
            session.nextval()
        # 重复 close 幂等
        session.close()

    def test_context_manager_closes_session(self):
        catalog = SequenceCatalog()
        catalog.create_sequence("s", cache_size=2)
        with catalog.open_session("s") as session:
            self.assertEqual(session.nextval(), 1)
        self.assertTrue(session.closed)

    def test_unconserved_numbers_leave_gap_on_close(self):
        catalog = SequenceCatalog()
        catalog.create_sequence("s", start_value=1, increment_by=1,
                                min_value=1, max_value=100, cache_size=5)
        first = catalog.open_session("s")
        self.assertEqual(first.nextval(), 1)
        first.close()  # 2..5 丢弃
        second = catalog.open_session("s")
        # watermark 已在 5，新会话从 6 开始，2..5 空缺
        self.assertEqual(second.nextval(), 6)
        second.close()

    def test_two_sessions_get_disjoint_batches(self):
        catalog = SequenceCatalog()
        catalog.create_sequence("s", start_value=1, increment_by=1,
                                min_value=1, max_value=100, cache_size=4)
        a = catalog.open_session("s")
        b = catalog.open_session("s")
        a_values = [a.nextval() for _ in range(4)]
        b_values = [b.nextval() for _ in range(4)]
        self.assertEqual(a_values, [1, 2, 3, 4])
        self.assertEqual(b_values, [5, 6, 7, 8])
        self.assertEqual(len(set(a_values) & set(b_values)), 0)
        a.close()
        b.close()


if __name__ == "__main__":
    unittest.main()
