import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from merge_operator import (
    DoubleAckError, Eos, ListSource, MergeOperator, OutputCommitter, Row,
    StaleBatchError, UnknownBatchError,
)


class CommitterTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="merge_commit_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_half_committed_batch_aborted_on_recover(self):
        """emit 后未 ack 未 checkpoint 即崩溃：半次提交的批次作废，不能重复确认。"""
        s0 = ListSource([Row(1, 1), Row(2, 2)])
        op = MergeOperator([s0], self.dir, memory_limit=10)
        b1 = op.emit_ready()
        self.assertEqual([r.key for r in b1], [1])
        op.ack(op.last_batch_id)      # 批次 1 确认
        op.checkpoint()               # 检查点视野 = 批次 1

        s0.extend([Row(3, 3), Eos()])
        b2 = op.emit_ready()
        self.assertEqual([r.key for r in b2], [2, 3])
        half_batch = op.last_batch_id  # 批次 2：emit 后崩溃，未 ack 未 checkpoint

        # 崩溃恢复
        s0b = ListSource([Row(1, 1), Row(2, 2), Row(3, 3), Eos()])
        op2 = MergeOperator.recover([s0b], self.dir, memory_limit=10)
        self.assertTrue(op2.committer.is_aborted(half_batch))
        self.assertEqual(op2.pending(), [])
        # 半次提交的批次不能再次被确认
        with self.assertRaises(StaleBatchError):
            op2.ack(half_batch)
        # 已确认的批次也不能重复确认
        with self.assertRaises(DoubleAckError):
            op2.ack(1)
        # 算子从检查点读位置重发同样的行，不丢失
        b3 = op2.emit_ready()
        self.assertEqual([r.key for r in b3], [2, 3])
        op2.ack(op2.last_batch_id)    # 新批次确认成功
        self.assertEqual(op2.pending(), [])
        self.assertTrue(op2.exhausted)

    def test_pending_batch_redeliverable_after_recover(self):
        """emit 并 checkpoint 后、ack 前崩溃：批次仍有效，恢复后可补确认。"""
        s0 = ListSource([Row(1, 1), Row(2, 2), Eos()])
        op = MergeOperator([s0], self.dir)
        b1 = op.emit_ready()
        self.assertEqual([r.key for r in b1], [1, 2])
        op.checkpoint()               # 检查点视野覆盖批次 1
        # 崩溃：批次 1 未确认

        s0b = ListSource([Row(1, 1), Row(2, 2), Eos()])
        op2 = MergeOperator.recover([s0b], self.dir)
        pending = op2.pending()
        self.assertEqual(len(pending), 1)
        bid, rows = pending[0]
        self.assertEqual([r.key for r in rows], [1, 2])  # 可重新投递给下游
        op2.ack(bid)                                       # 补确认成功
        self.assertEqual(op2.pending(), [])
        with self.assertRaises(DoubleAckError):
            op2.ack(bid)

    def test_double_ack_rejected(self):
        op = MergeOperator([ListSource([Row(1, 1), Eos()])], self.dir)
        op.emit_ready()
        op.ack(op.last_batch_id)
        with self.assertRaises(DoubleAckError):
            op.ack(op.last_batch_id)

    def test_unknown_batch_ack_rejected(self):
        op = MergeOperator([ListSource([Row(1, 1), Eos()])], self.dir)
        op.emit_ready()
        with self.assertRaises(UnknownBatchError):
            op.ack(999)

    def test_torn_wal_tail_ignored(self):
        """WAL 尾部撕裂（半次写入）在恢复时被丢弃，已有记录不受影响。"""
        op = MergeOperator([ListSource([Row(1, 1), Eos()])], self.dir)
        op.emit_ready()
        op.ack(op.last_batch_id)
        op.checkpoint()
        wal = os.path.join(self.dir, OutputCommitter.WAL_FILE)
        with open(wal, "a", encoding="utf-8") as fh:
            fh.write('{"op": "emit", "id": 99')  # 撕裂的半行
        op2 = MergeOperator.recover([ListSource([Row(1, 1), Eos()])], self.dir)
        self.assertTrue(op2.committer.is_acked(1))
        self.assertEqual(op2.pending(), [])
        with self.assertRaises(UnknownBatchError):
            op2.ack(99)  # 撕裂记录不存在，不能确认


if __name__ == "__main__":
    unittest.main()
