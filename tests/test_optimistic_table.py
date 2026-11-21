"""optimistic_table 的 unittest 测试。

场景来源题面：正常提交、快照读、写-写冲突、范围读冲突（进入/离开/
范围内改分/删除/幻影）、其他列改写不冲突、25->15->25 往返仍冲突、
范围外改分并行提交、交错时序。
"""

import unittest

from optimistic_table import ConflictError, Table, TransactionStateError


def seeded_table():
    """构造基础数据：范围内两行、范围外两行。"""
    t = Table()
    tx = t.begin()
    tx.write("a", score=12, name="alpha")
    tx.write("b", score=18, name="bravo")
    tx.write("c", score=25, name="charlie")
    tx.write("d", score=5, name="delta")
    tx.commit()
    return t


class TestBasicCommitAndSnapshot(unittest.TestCase):
    def test_commit_applies_writes(self):
        t = Table()
        tx = t.begin()
        tx.write("x", score=15, name="x-ray")
        version = tx.commit()
        self.assertEqual(version, 1)
        self.assertEqual(tx.state, "committed")
        self.assertEqual(t.snapshot_rows(), {"x": {"score": 15, "name": "x-ray"}})

    def test_read_range_filters_and_sorts(self):
        t = seeded_table()
        tx = t.begin()
        rows = tx.read_range(10, 20)
        self.assertEqual([pk for pk, _ in rows], ["a", "b"])
        self.assertEqual(rows[0][1]["score"], 12)
        tx.commit()

    def test_read_your_own_writes(self):
        t = seeded_table()
        tx = t.begin()
        tx.write("c", score=15)  # 自己把 c 移入范围
        rows = tx.read_range(10, 20)
        self.assertEqual([pk for pk, _ in rows], ["a", "b", "c"])
        tx.abort = getattr(tx, "abort", None)  # 无需 abort；直接提交
        tx.commit()
        self.assertEqual(t.snapshot_rows()["c"]["score"], 15)

    def test_snapshot_isolation_from_later_commits(self):
        t = seeded_table()
        reader = t.begin()
        writer = t.begin()
        writer.write("c", score=11)  # 外部提交把 c 移入范围
        writer.commit()
        # reader 仍读开始快照，看不到 c 的新分值
        rows = reader.read_range(10, 20)
        self.assertEqual([pk for pk, _ in rows], ["a", "b"])

    def test_read_only_transaction_commits(self):
        t = seeded_table()
        tx = t.begin()
        tx.read_range(10, 20)
        self.assertEqual(tx.commit(), t.version)
        self.assertEqual(tx.state, "committed")


class TestWriteWriteConflict(unittest.TestCase):
    def test_same_pk_written_by_later_commit_rejected(self):
        t = seeded_table()
        t1 = t.begin()
        t2 = t.begin()
        t2.write("a", score=13)
        v2 = t2.commit()
        t1.write("a", score=14)
        with self.assertRaises(ConflictError) as ctx:
            t1.commit()
        self.assertEqual(ctx.exception.commit_version, v2)
        self.assertEqual(ctx.exception.pk, "a")
        self.assertEqual(ctx.exception.reason, "write-write")
        self.assertEqual(t1.state, "aborted")
        # 表数据保持 t2 的结果
        self.assertEqual(t.snapshot_rows()["a"]["score"], 13)

    def test_disjoint_write_sets_both_commit(self):
        t = seeded_table()
        t1 = t.begin()
        t2 = t.begin()
        t1.write("a", score=13)
        t2.write("d", score=6)
        t1.commit()
        t2.commit()  # 主键不相交，无冲突
        self.assertEqual(t.snapshot_rows()["a"]["score"], 13)
        self.assertEqual(t.snapshot_rows()["d"]["score"], 6)


class TestReadRangeConflict(unittest.TestCase):
    def test_row_entering_range_conflicts(self):
        t = seeded_table()
        reader = t.begin()
        reader.read_range(10, 20)  # 读到 a, b
        mover = t.begin()
        mover.write("c", score=15)  # c: 25 -> 15，进入范围
        v = mover.commit()
        reader.write("d", name="delta-updated")  # 根据读结果写另一行
        with self.assertRaises(ConflictError) as ctx:
            reader.commit()
        self.assertEqual(ctx.exception.commit_version, v)
        self.assertEqual(ctx.exception.pk, "c")
        self.assertEqual(ctx.exception.reason, "read-range")

    def test_row_leaving_range_conflicts(self):
        t = seeded_table()
        reader = t.begin()
        reader.read_range(10, 20)
        mover = t.begin()
        mover.write("a", score=25)  # a: 12 -> 25，离开范围
        v = mover.commit()
        reader.write("d", name="x")
        with self.assertRaises(ConflictError) as ctx:
            reader.commit()
        self.assertEqual(ctx.exception.commit_version, v)
        self.assertEqual(ctx.exception.pk, "a")

    def test_score_change_within_range_conflicts(self):
        t = seeded_table()
        reader = t.begin()
        reader.read_range(10, 20)
        mover = t.begin()
        mover.write("a", score=14)  # 12 -> 14，仍在范围内但分值集合变了
        v = mover.commit()
        reader.write("d", name="x")
        with self.assertRaises(ConflictError) as ctx:
            reader.commit()
        self.assertEqual(ctx.exception.commit_version, v)
        self.assertEqual(ctx.exception.pk, "a")

    def test_delete_of_in_range_row_conflicts(self):
        t = seeded_table()
        reader = t.begin()
        reader.read_range(10, 20)
        deleter = t.begin()
        deleter.delete("b")
        v = deleter.commit()
        reader.write("d", name="x")
        with self.assertRaises(ConflictError) as ctx:
            reader.commit()
        self.assertEqual(ctx.exception.commit_version, v)
        self.assertEqual(ctx.exception.pk, "b")

    def test_empty_initial_read_still_registered(self):
        t = Table()  # 空表，初读必为空
        reader = t.begin()
        self.assertEqual(reader.read_range(10, 20), [])
        inserter = t.begin()
        inserter.write("new", score=12)  # 幻影：新行进入空范围
        v = inserter.commit()
        reader.write("other", score=99)
        with self.assertRaises(ConflictError) as ctx:
            reader.commit()
        self.assertEqual(ctx.exception.commit_version, v)
        self.assertEqual(ctx.exception.pk, "new")

    def test_move_out_and_back_still_conflicts(self):
        """行 25 -> 15 再由另一笔移回 25：按每次提交净效果校验，读者仍冲突。"""
        t = seeded_table()
        reader = t.begin()
        reader.read_range(10, 20)
        m1 = t.begin()
        m1.write("c", score=15)  # 25 -> 15：进入范围
        v1 = m1.commit()
        m2 = t.begin()
        m2.write("c", score=25)  # 15 -> 25：又移回
        m2.commit()
        reader.write("d", name="x")
        with self.assertRaises(ConflictError) as ctx:
            reader.commit()
        # 报告的是第一笔造成净变化的提交
        self.assertEqual(ctx.exception.commit_version, v1)
        self.assertEqual(ctx.exception.pk, "c")
        # 最终值虽与快照相同，冲突依然成立
        self.assertEqual(t.snapshot_rows()["c"]["score"], 25)


class TestNoConflictCases(unittest.TestCase):
    def test_other_column_rewrite_does_not_conflict(self):
        t = seeded_table()
        reader = t.begin()
        reader.read_range(10, 20)
        other = t.begin()
        other.write("a", name="alpha-renamed")  # 只改其他列，分值不动
        other.commit()
        reader.write("d", name="x")
        reader.commit()  # 不冲突
        self.assertEqual(reader.state, "committed")

    def test_out_of_range_score_change_commits_in_parallel(self):
        t = seeded_table()
        reader = t.begin()
        reader.read_range(10, 20)
        outside = t.begin()
        outside.write("c", score=28)  # 25 -> 28，完全在范围外
        outside.commit()
        reader.write("d", name="x")
        reader.commit()  # 并行提交成功
        self.assertEqual(t.snapshot_rows()["c"]["score"], 28)
        self.assertEqual(t.snapshot_rows()["d"]["name"], "x")

    def test_boundary_scores(self):
        """边界：10 在范围内，20 不在；9->10 与 20->19 的净效果相反。"""
        t = Table()
        setup = t.begin()
        setup.write("lo", score=9)
        setup.write("hi", score=20)
        setup.commit()

        reader = t.begin()
        self.assertEqual(reader.read_range(10, 20), [])  # 边界均为空

        mover = t.begin()
        mover.write("lo", score=10)  # 9 -> 10：进入范围（下界闭）
        v = mover.commit()
        reader.write("z", score=99)
        with self.assertRaises(ConflictError) as ctx:
            reader.commit()
        self.assertEqual(ctx.exception.pk, "lo")
        self.assertEqual(ctx.exception.commit_version, v)

        # 另一组：20 -> 19 进入范围（上界开）
        t2 = Table()
        s2 = t2.begin()
        s2.write("hi", score=20)
        s2.commit()
        r2 = t2.begin()
        r2.read_range(10, 20)
        m2 = t2.begin()
        m2.write("hi", score=19)
        m2.commit()
        r2.write("z", score=1)
        with self.assertRaises(ConflictError):
            r2.commit()


class TestInterleavedScenario(unittest.TestCase):
    def test_interleaved_readers_and_writers(self):
        """交错时序：读者读范围 -> 范围外事务并行提交成功 -> 范围内移动导致读者冲突。"""
        t = seeded_table()
        reader = t.begin()
        self.assertEqual([pk for pk, _ in reader.read_range(10, 20)], ["a", "b"])

        # 范围外改分的事务：与读者并行，成功提交
        outside = t.begin()
        outside.write("c", score=30)
        outside.commit()

        # 另一事务把 d 移入范围
        mover = t.begin()
        mover.write("d", score=11)
        v_mover = mover.commit()

        # 读者根据读到的 (主键, 分值) 写另一行 -> 被拒绝
        reader.write("log", source="a:12,b:18")
        with self.assertRaises(ConflictError) as ctx:
            reader.commit()
        self.assertEqual(ctx.exception.commit_version, v_mover)
        self.assertEqual(ctx.exception.pk, "d")
        self.assertEqual(reader.state, "aborted")

        # 冲突读者的写入未生效，范围外事务的写入已生效
        rows = t.snapshot_rows()
        self.assertNotIn("log", rows)
        self.assertEqual(rows["c"]["score"], 30)
        self.assertEqual(rows["d"]["score"], 11)

    def test_multiple_range_reads_all_validated(self):
        t = seeded_table()
        reader = t.begin()
        reader.read_range(10, 20)
        reader.read_range(0, 100)  # 第二个范围读也要登记
        other = t.begin()
        other.write("c", score=50)  # 25 -> 50：不影响 [10,20)，但影响 [0,100)
        v = other.commit()
        reader.write("d", name="x")
        with self.assertRaises(ConflictError) as ctx:
            reader.commit()
        self.assertEqual(ctx.exception.commit_version, v)
        self.assertEqual(ctx.exception.pk, "c")


class TestTransactionLifecycle(unittest.TestCase):
    def test_use_after_commit_rejected(self):
        t = seeded_table()
        tx = t.begin()
        tx.commit()
        with self.assertRaises(TransactionStateError):
            tx.write("a", score=1)
        with self.assertRaises(TransactionStateError):
            tx.read_range(10, 20)
        with self.assertRaises(TransactionStateError):
            tx.commit()

    def test_use_after_abort_rejected(self):
        t = seeded_table()
        t1 = t.begin()
        t2 = t.begin()
        t2.write("a", score=13)
        t2.commit()
        t1.write("a", score=14)
        with self.assertRaises(ConflictError):
            t1.commit()
        with self.assertRaises(TransactionStateError):
            t1.write("b", score=1)


if __name__ == "__main__":
    unittest.main()
