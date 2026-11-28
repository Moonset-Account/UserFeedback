import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collation import Collation, SortKey
from migration import Migration, NewIndex, OldIndex


def make_collation() -> Collation:
    c = Collation(levels=2)
    c.add("a", ((1,), (1,)))
    c.add("e", ((2,), (1,)))
    c.add("c", ((3,), (1,)))
    c.add("h", ((4,), (2,)))
    c.add("ch", ((10,), (5,)))       # 收缩
    c.add("dz", ((10,), (9,)))       # 与 "ch" 主权重相同
    c.add("æ", ((1, 2), (1, 1)))     # 展开，与 "ae" 排序键相等
    return c


def make_old_index() -> OldIndex:
    old = OldIndex()
    old.insert("ae", {"id": 1})
    old.insert("beta", {"id": 2})
    old.insert("ch", {"id": 3})
    return old


class TestChunkBoundaryScan(unittest.TestCase):
    """扫描期间名字按 chunk_size=1 分块喂入，收缩 "ch" 跨过块边界。"""

    def test_scan_with_contraction_across_chunks(self):
        col = make_collation()
        old = make_old_index()
        mig = Migration(old, col, chunk_size=1)
        mig.begin_scan()
        # "ch" 被逐字符喂入，仍匹配收缩规则而不是 "c"+"h"。
        self.assertEqual(mig._scanned["ch"], SortKey(((10,), (5,))))
        self.assertEqual(mig._scanned["ch"], col.sort_key("ch"))
        self.assertNotEqual(mig._scanned["ch"], SortKey(((3, 4), (1, 2))))


class TestSwitchWithPostScanRenameConflict(unittest.TestCase):
    """题面要求的场景：收缩跨分块边界 + 冲突行在扫描后被改名。"""

    def test_conflict_rename_after_scan_aborts_and_old_index_stays(self):
        col = make_collation()
        old = make_old_index()
        mig = Migration(old, col, chunk_size=1)
        mig.begin_scan()

        # 扫描之后的改动：插入一行、把 "beta" 改名为 "æ"。
        # "æ" 展开后与扫描时已存在的 "ae" 排序键完全相等。
        mig.insert("delta", {"id": 4})
        mig.rename("beta", "æ")

        ok = mig.commit()
        self.assertFalse(ok)
        self.assertEqual(mig.state, Migration.ABORTED)
        self.assertIn(("ae", "æ"), mig.conflicts)

        # 旧索引保持可查，且扫描后的改动在旧索引中仍然可见。
        self.assertIs(mig.active_index, old)
        self.assertEqual(old.lookup("ae"), {"id": 1})
        self.assertEqual(old.lookup("æ"), {"id": 2})
        self.assertEqual(old.lookup("delta"), {"id": 4})
        self.assertIsNone(old.lookup("beta"))


class TestSuccessfulSwitch(unittest.TestCase):
    def test_post_scan_changes_included_in_new_index(self):
        col = make_collation()
        old = OldIndex()
        old.insert("alpha", {"id": 1})
        old.insert("ch", {"id": 2})
        mig = Migration(old, col, chunk_size=1)
        mig.begin_scan()

        # 扫描后的改动：改名 + 插入，均不冲突。
        mig.rename("alpha", "a")
        mig.insert("dz", {"id": 3})

        ok = mig.commit()
        self.assertTrue(ok)
        self.assertEqual(mig.state, Migration.SWITCHED)

        new = mig.active_index
        self.assertIsInstance(new, NewIndex)
        # 新索引包含扫描后的改动。
        self.assertEqual(new.lookup("a"), {"id": 1})
        self.assertEqual(new.lookup("dz"), {"id": 3})
        self.assertNotIn("alpha", new)
        # 新排序规则：主权重 1 < 10；"ch" 与 "dz" 主权重相同，次权重 5 < 9。
        self.assertEqual(new.sorted_names(), ["a", "ch", "dz"])
        self.assertEqual(new.sort_key_of("ch"), SortKey(((10,), (5,))))

    def test_rename_of_post_scan_insert(self):
        # 增量日志叠加：扫描后插入的行又被改名。
        col = make_collation()
        old = OldIndex()
        old.insert("ae", {"id": 1})
        mig = Migration(old, col)
        mig.begin_scan()
        mig.insert("tmp", {"id": 2})
        mig.rename("tmp", "final")

        self.assertTrue(mig.commit())
        new = mig.active_index
        self.assertIn("final", new)
        self.assertNotIn("tmp", new)
        self.assertEqual(new.lookup("final"), {"id": 2})


class TestConflictDetection(unittest.TestCase):
    def test_post_scan_insert_conflicting_with_scanned_name(self):
        col = make_collation()
        old = OldIndex()
        old.insert("ae", {"id": 1})
        mig = Migration(old, col)
        mig.begin_scan()
        mig.insert("æ", {"id": 2})  # 展开后与 "ae" 冲突

        self.assertFalse(mig.commit())
        self.assertEqual(mig.state, Migration.ABORTED)
        self.assertIs(mig.active_index, old)
        self.assertEqual(old.lookup("æ"), {"id": 2})

    def test_preexisting_conflict_found_at_scan(self):
        # 旧索引按精确匹配允许 "ae" 与 "æ" 共存，新规则下冲突。
        col = make_collation()
        old = OldIndex()
        old.insert("ae", {"id": 1})
        old.insert("æ", {"id": 2})
        mig = Migration(old, col)
        mig.begin_scan()

        self.assertFalse(mig.commit())
        self.assertIn(("ae", "æ"), mig.conflicts)
        self.assertIs(mig.active_index, old)

    def test_commit_requires_scan(self):
        col = make_collation()
        old = make_old_index()
        mig = Migration(old, col)
        with self.assertRaises(RuntimeError):
            mig.commit()


if __name__ == "__main__":
    unittest.main()
