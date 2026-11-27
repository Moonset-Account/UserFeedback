import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from merge_operator import Eos, ListSource, MergeOperator, Row
from verify import expected_merge, verify_output


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="merge_verify_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_verify_accepts_correct_merge(self):
        s0_rows = [Row(1, 1), Row(4, 4), Row(5, 6)]
        s1_rows = [Row(2, 2), Row(4, 3), Row(5, 5)]
        op = MergeOperator([ListSource(s0_rows + [Eos()]),
                            ListSource(s1_rows + [Eos()])], self.dir)
        out = []
        while not op.exhausted:
            batch = op.emit_ready()
            if not batch:
                break
            out.extend(batch)
        report = verify_output(expected_merge([s0_rows, s1_rows]), out)
        self.assertTrue(report.ok, report)
        self.assertIsNone(report.first_divergence_index)

    def test_verify_detects_misguided_hole_passage(self):
        """误过空洞导致的顺序变动可被检测：旧行提前输出，重算行后至。"""
        # 最终事实（重算完成后）：分片 0 的键 10/11 是重算后的新行
        final_s0 = [Row(1, 1), Row(10, 5), Row(11, 6)]
        final_s1 = [Row(2, 2), Row(10, 7), Row(12, 8)]
        expected = expected_merge([final_s0, final_s1])

        # 有缺陷的执行：空洞被误过，重算前的旧行 (seq 3,4) 先输出，
        # 重算后的新行 (seq 5,6,7) 被追加在更大的键之后
        buggy_actual = [Row(1, 1), Row(2, 2), Row(10, 3), Row(11, 4),
                        Row(12, 8), Row(10, 5), Row(11, 6), Row(10, 7)]
        report = verify_output(expected, buggy_actual)
        self.assertFalse(report.ok)
        # 顺序回退被定位：键 10 出现在键 12 之后
        kinds = [d.kind for d in report.discrepancies]
        self.assertIn("order", kinds)
        order_disc = next(d for d in report.discrepancies if d.kind == "order")
        self.assertEqual(order_disc.index, 5)
        # 内容偏差：重算前的旧行多余（重算行最终也输出了，故无缺失）
        self.assertEqual(report.extra_seqs, [3, 4])
        self.assertEqual(report.missing_seqs, [])
        # 首个分叉位置
        self.assertEqual(report.first_divergence_index, 2)

    def test_verify_detects_duplicate_output(self):
        expected = [Row(1, 1), Row(2, 2)]
        actual = [Row(1, 1), Row(1, 1), Row(2, 2)]  # 半次提交被重复确认的效果
        report = verify_output(expected, actual)
        self.assertFalse(report.ok)
        self.assertIn("duplicate", [d.kind for d in report.discrepancies])
        self.assertEqual(report.extra_seqs, [1])

    def test_verify_detects_missing_rows(self):
        expected = [Row(1, 1), Row(2, 2), Row(3, 3)]
        actual = [Row(1, 1), Row(3, 3)]
        report = verify_output(expected, actual)
        self.assertFalse(report.ok)
        self.assertEqual(report.missing_seqs, [2])
        self.assertEqual(report.first_divergence_index, 1)


if __name__ == "__main__":
    unittest.main()
