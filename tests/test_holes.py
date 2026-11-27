import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from merge_operator import (
    Eos, HoleClosed, HoleOpen, HoleRegressionError, ListSource, MergeOperator,
    Row, SourceOrderError, SourceProtocolError,
)
from verify import expected_merge, verify_output


class HoleTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="merge_hole_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_hole_blocks_only_covered_keys(self):
        """空洞只阻塞其覆盖的键区间，其余分片小于下界的行继续输出。"""
        s0 = ListSource([Row(1, 1), Row(2, 2), HoleOpen(10)])  # 之后在重算
        s1 = ListSource([Row(1, 3), Row(5, 4), Row(12, 5), Row(20, 6), Eos()])
        op = MergeOperator([s0, s1], self.dir)

        out1 = op.emit_ready()
        self.assertEqual([r.key for r in out1], [1, 1, 2, 5])  # 12/20 被空洞挡住
        self.assertFalse(op.exhausted)

        s0.extend([HoleClosed(), Row(10, 7), Row(15, 8), Eos()])
        out2 = op.emit_ready()
        self.assertEqual([r.key for r in out2], [10, 12, 15, 20])
        self.assertTrue(op.exhausted)

        report = verify_output(
            expected_merge([
                [Row(1, 1), Row(2, 2), Row(10, 7), Row(15, 8)],
                [Row(1, 3), Row(5, 4), Row(12, 5), Row(20, 6)],
            ]), out1 + out2)
        self.assertTrue(report.ok, report)

    def test_unbounded_hole_blocks_everything(self):
        """无界下界的空洞阻塞该分片参与的一切输出。"""
        s0 = ListSource([HoleOpen(None)])  # 整个分片在重算
        s1 = ListSource([Row(1, 1), Row(2, 2), Eos()])
        op = MergeOperator([s0, s1], self.dir)
        self.assertEqual(op.emit_ready(), [])

        s0.extend([HoleClosed(), Row(0, 3), Eos()])
        out = op.emit_ready()
        self.assertEqual([r.key for r in out], [0, 1, 2])
        self.assertTrue(op.exhausted)

    def test_same_key_across_hole_boundary(self):
        """相同键跨空洞边界：空洞前已投递的键 L 行与重算后的键 L 行按 seq 合并。"""
        s0 = ListSource([Row(3, 1), Row(5, 10), HoleOpen(5)])
        s1 = ListSource([Row(5, 11), Row(6, 30), Eos()])
        op = MergeOperator([s0, s1], self.dir)

        out1 = op.emit_ready()
        self.assertEqual([r.key for r in out1], [3])  # 键 5 被空洞挡住

        s0.extend([HoleClosed(), Row(5, 12), Row(5, 13), Row(8, 40), Eos()])
        out2 = op.emit_ready()
        self.assertEqual([r.key for r in out2], [5, 5, 5, 5, 6, 8])
        # 空洞前的键 5 行（seq=10）与重算后的键 5 行（seq=12,13）按 seq 稳定排序
        self.assertEqual([r.seq for r in out2 if r.key == 5], [10, 11, 12, 13])

    def test_already_emitted_prefix_not_reemitted(self):
        """空洞下界之前已输出的前缀不会被重算影响，也不会重复输出。"""
        s0 = ListSource([Row(1, 1), Row(2, 2), Row(9, 9), HoleOpen(9)])
        s1 = ListSource([Row(1, 3), Row(2, 4), Eos()])
        op = MergeOperator([s0, s1], self.dir)

        out1 = op.emit_ready()
        self.assertEqual([r.key for r in out1], [1, 1, 2, 2])  # 前缀 < 9

        s0.extend([HoleClosed(), Row(9, 10), Row(10, 11), Eos()])
        out2 = op.emit_ready()
        self.assertEqual([r.key for r in out2], [9, 9, 10])

        all_seqs = [r.seq for r in out1 + out2]
        self.assertEqual(len(all_seqs), len(set(all_seqs)))  # 无重复输出
        self.assertEqual([r.key for r in out1 + out2],
                         sorted(r.key for r in out1 + out2))  # 整体有序

    def test_unbounded_hole_after_emission_rejected(self):
        """已有输出后再开无界空洞会覆盖已输出前缀，必须拒绝。"""
        s0 = ListSource([Row(1, 1), Eos()])
        s1 = ListSource([Row(2, 2), Row(3, 3)])
        op = MergeOperator([s0, s1], self.dir)
        self.assertEqual([r.key for r in op.emit_ready()], [1, 2])
        s1.extend([HoleOpen(None)])
        with self.assertRaises(HoleRegressionError):
            op.emit_ready()

    def test_hole_bound_covering_emitted_key_rejected(self):
        """空洞下界 <= 已输出键时拒绝（重算会改写已输出的键组）。"""
        s0 = ListSource([Row(1, 1), Row(5, 5), Eos()])
        s1 = ListSource([Row(2, 2), Row(4, 4)])
        op = MergeOperator([s0, s1], self.dir)
        self.assertEqual([r.key for r in op.emit_ready()], [1, 2])
        s1.extend([HoleOpen(2)])  # 键 2 已经输出过了
        with self.assertRaises(HoleRegressionError):
            op.emit_ready()

    def test_hole_bound_below_delivered_key_rejected(self):
        """空洞下界低于该分片已投递的键，违反 frontier 承诺。"""
        s0 = ListSource([Row(5, 1)])
        s1 = ListSource([Eos()])
        op = MergeOperator([s0, s1], self.dir)
        op.emit_ready()  # 键 5 被 frontier 挡住，尚未输出
        s0.extend([HoleOpen(3)])
        with self.assertRaises(SourceProtocolError):
            op.emit_ready()

    def test_row_during_open_hole_rejected(self):
        s0 = ListSource([Row(1, 1), HoleOpen(5), Row(2, 2)])
        op = MergeOperator([s0], self.dir)
        with self.assertRaises(SourceProtocolError):
            op.emit_ready()

    def test_row_below_closed_hole_bound_rejected(self):
        """空洞关闭后投递比重算下界还小的键，违反承诺。"""
        s0 = ListSource([Row(1, 1), HoleOpen(10), HoleClosed(), Row(7, 2)])
        op = MergeOperator([s0], self.dir)
        with self.assertRaises(SourceProtocolError):
            op.emit_ready()

    def test_eos_with_open_hole_rejected(self):
        s0 = ListSource([HoleOpen(5), Eos()])
        op = MergeOperator([s0], self.dir)
        with self.assertRaises(SourceProtocolError):
            op.emit_ready()

    def test_stray_hole_closed_rejected(self):
        s0 = ListSource([HoleClosed()])
        op = MergeOperator([s0], self.dir)
        with self.assertRaises(SourceProtocolError):
            op.emit_ready()

    def test_misguided_hole_passage_detected(self):
        """误过空洞：源未标空洞就投递将被重算的旧行，重算结果键序回退可被算子检测。"""
        s0 = ListSource([Row(1, 1), Row(10, 2), Row(11, 3)])  # 旧行未标空洞
        s1 = ListSource([Row(2, 4), Eos()])
        op = MergeOperator([s0, s1], self.dir)
        out1 = op.emit_ready()
        # frontier=11：键 < 11 的行（含被误放行的旧行 10）被输出，键 11 仍被挡住
        self.assertEqual([r.key for r in out1], [1, 2, 10])
        # 重算结果键 5 小于已投递的 11：顺序变动被检测为源键序回退
        s0.extend([Row(5, 5)])
        with self.assertRaises(SourceOrderError):
            op.emit_ready()


if __name__ == "__main__":
    unittest.main()
