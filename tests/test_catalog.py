"""目录生命周期与跨序列隔离测试。"""

import unittest

from sequence_allocator import (
    SequenceCatalog,
    SequenceNotFoundError,
    SequenceSessionClosedError,
)


class TestCatalogLifecycle(unittest.TestCase):
    def test_missing_sequence_raises(self):
        catalog = SequenceCatalog()
        with self.assertRaises(SequenceNotFoundError):
            catalog.open_session("nope")
        with self.assertRaises(SequenceNotFoundError):
            catalog.alter_increment("nope", 2)
        with self.assertRaises(SequenceNotFoundError):
            catalog.get_state("nope")

    def test_drop_closes_open_sessions(self):
        catalog = SequenceCatalog()
        catalog.create_sequence("s", cache_size=3)
        session = catalog.open_session("s")
        self.assertEqual(session.nextval(), 1)
        catalog.drop_sequence("s")
        self.assertTrue(session.closed)
        with self.assertRaises(SequenceSessionClosedError):
            session.nextval()
        self.assertNotIn("s", catalog.list_sequences())

    def test_sequences_are_isolated(self):
        catalog = SequenceCatalog()
        catalog.create_sequence("a", start_value=1, cache_size=2, max_value=100)
        catalog.create_sequence("b", start_value=100, increment_by=-10,
                                min_value=0, max_value=100, cache_size=2)
        sa = catalog.open_session("a")
        sb = catalog.open_session("b")
        self.assertEqual(sa.nextval(), 1)
        self.assertEqual(sb.nextval(), 100)
        catalog.alter_increment("a", 7)
        self.assertEqual(sa.nextval(), 9)   # watermark 2 + 7
        self.assertEqual(sb.nextval(), 90)  # b 完全不受影响
        self.assertEqual(catalog.get_state("b").version, 1)
        sa.close()
        sb.close()

    def test_state_snapshot_is_independent_copy(self):
        catalog = SequenceCatalog()
        catalog.create_sequence("s", cache_size=2)
        snapshot = catalog.get_state("s")
        snapshot.watermark = 999
        self.assertEqual(catalog.get_state("s").watermark, 0)  # 初始 = start - step


if __name__ == "__main__":
    unittest.main()
