import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from merge_operator import (
    Eos, HoleOpen, ListSource, MergeError, MergeOperator, Row, SourceOrderError,
)
from verify import expected_merge, verify_output


def drain(op):
    """拉取全部可输出行并逐批确认；返回输出行列表。"""
    out = []
    while not op.exhausted:
        batch = op.emit_ready()
        if not batch:
            break
        op.ack(op.last_batch_id)
        out.extend(batch)
    return out


class MergeBasicTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="merge_basic_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_basic_multiway_merge(self):
        s0 = ListSource([Row(1, 101, "a"), Row(4, 104), Row(7, 107), Eos()])
        s1 = ListSource([Row(1, 102), Row(2, 105), Row(7, 108), Eos()])
        s2 = ListSource([Row(3, 103), Row(7, 106), Eos()])
        op = MergeOperator([s0, s1, s2], self.dir)
        out = drain(op)
        self.assertTrue(op.exhausted)
        self.assertEqual([r.key for r in out], [1, 1, 2, 3, 4, 7, 7, 7])
        # 相等键按全局输入序号稳定排序
        self.assertEqual([r.seq for r in out if r.key == 1], [101, 102])
        self.assertEqual([r.seq for r in out if r.key == 7], [106, 107, 108])
        self.assertEqual(out[0].payload, "a")  # payload 保留
        report = verify_output(
            expected_merge([
                [Row(1, 101, "a"), Row(4, 104), Row(7, 107)],
                [Row(1, 102), Row(2, 105), Row(7, 108)],
                [Row(3, 103), Row(7, 106)],
            ]), out)
        self.assertTrue(report.ok, report)

    def test_equal_keys_stable_by_global_seq(self):
        s0 = ListSource([Row(5, 10), Row(5, 12), Row(5, 14), Eos()])
        s1 = ListSource([Row(5, 11), Row(5, 13), Eos()])
        op = MergeOperator([s0, s1], self.dir)
        out = drain(op)
        self.assertEqual([r.seq for r in out], [10, 11, 12, 13, 14])

    def test_single_shard_passthrough(self):
        s0 = ListSource([Row(i, i) for i in range(5)] + [Eos()])
        op = MergeOperator([s0], self.dir)
        out = drain(op)
        self.assertEqual([r.key for r in out], [0, 1, 2, 3, 4])

    def test_empty_sources(self):
        op = MergeOperator([ListSource([Eos()]), ListSource([Eos()])], self.dir)
        self.assertEqual(op.emit_ready(), [])
        self.assertTrue(op.exhausted)

    def test_source_order_violation_rejected(self):
        s0 = ListSource([Row(5, 1), Row(3, 2), Eos()])
        op = MergeOperator([s0], self.dir)
        with self.assertRaises(SourceOrderError):
            op.emit_ready()

    def test_constructor_refuses_existing_checkpoint(self):
        op = MergeOperator([ListSource([Eos()])], self.dir)
        op.checkpoint()
        with self.assertRaises(MergeError):
            MergeOperator([ListSource([Eos()])], self.dir)

    def test_recover_without_checkpoint_raises(self):
        with self.assertRaises(MergeError):
            MergeOperator.recover([ListSource([Eos()])], self.dir)

    def test_recover_with_wrong_shard_count_raises(self):
        op = MergeOperator([ListSource([Eos()])], self.dir)
        op.checkpoint()
        with self.assertRaises(MergeError):
            MergeOperator.recover([ListSource(), ListSource()], self.dir)


if __name__ == "__main__":
    unittest.main()
