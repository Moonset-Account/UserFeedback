"""题面要求的综合交错场景：共享 + 互斥展开 + 分段 + 递归 + 匿名引用。

所有期望值都在测试注释中手工推导，防止“看起来合理”的回归。
"""

import sys
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from budget_guard import analyze, evaluate  # noqa: E402


def grand_graph():
    return {
        "id": "grand",
        "op": "seq",
        "steps": [
            # 唯一的共享元数据对象：64B / 2 records
            {"op": "alloc", "id": "meta", "bytes": 64, "records": 2,
             "share": "meta", "pos": "archive.json#meta"},
            {
                "op": "branch", "id": "strategy",
                "pos": "archive.json#strategy",
                "arms": [
                    {
                        "name": "linear",
                        "node": {
                            "op": "recurse", "id": "linear-pkg",
                            "depth": 3, "fan_out": 1,
                            "body": {"op": "seq", "steps": [
                                # 递归每一层都引用同一个 meta -> 0 新分配
                                {"op": "ref", "share": "meta", "bytes": 64},
                                {"op": "alloc", "bytes": 10, "records": 1},
                            ]},
                        },
                    },
                    {
                        "name": "bushy",
                        "node": {
                            "op": "recurse", "id": "bushy-pkg",
                            "depth": 3, "fan_out": 2,
                            # 实例数 1+2+4=7
                            "body": {"op": "alloc", "bytes": 5, "records": 2},
                        },
                    },
                ],
            },
            # 末尾匿名引用：不能假定共享 -> +8B / +1 record
            {"op": "ref", "id": "anon-tail", "bytes": 8, "records": 1,
             "pos": "archive.json#tail"},
        ],
    }


class TestGrandInterleaving(unittest.TestCase):
    def test_hand_computed_upper_bounds(self):
        rep = analyze(grand_graph())
        # linear arm: 3 层 × 10B/1rec = 30B / 3rec（meta ref 为 0）
        # bushy  arm: 7 实例 × 5B/2rec = 35B / 14rec
        # branch max = 35B / 14rec
        # seq: meta 64B/2rec + branch + anon 8B/1rec
        # total = 107B / 17rec
        self.assertEqual(rep.total.bytes, 107)
        self.assertEqual(rep.total.records, 17)
        # 只有一个匿名引用警告，没有阻断问题
        self.assertEqual(len(rep.warnings), 1)
        self.assertEqual(rep.blocking_issues, [])

    def test_budgets_independent_dimensions(self):
        rep = analyze(grand_graph())
        ok = evaluate(rep, max_bytes=107, max_records=17)
        self.assertTrue(ok.allowed)

        tight_bytes = evaluate(rep, max_bytes=106, max_records=100)
        self.assertFalse(tight_bytes.allowed)
        self.assertEqual(
            [v.code for v in tight_bytes.violations],
            ["BYTES_BUDGET_EXCEEDED"],
        )
        tight_records = evaluate(rep, max_bytes=10_000, max_records=16)
        self.assertFalse(tight_records.allowed)
        self.assertEqual(
            [v.code for v in tight_records.violations],
            ["RECORDS_BUDGET_EXCEEDED"],
        )

    def test_strict_mode_rejects_anonymous_warning(self):
        rep = analyze(grand_graph())
        self.assertTrue(evaluate(rep).allowed)
        strict = evaluate(rep, strict=True)
        self.assertFalse(strict.allowed)
        self.assertTrue(strict.warnings)

    def test_evidence_drives_segment_and_depth_together(self):
        g = {
            "op": "seq",
            "steps": [
                {"op": "segments", "id": "seg",
                 "count": {"evidence": "S"},
                 "body": {"op": "alloc", "bytes": 3, "records": 1}},
                {"op": "recurse", "id": "rec",
                 "depth": {"evidence": "D"}, "fan_out": 1,
                 "body": {"op": "alloc", "bytes": 5, "records": 2}},
            ],
        }
        # S=4 -> 12B/4rec ; D=2 -> 10B/4rec ; total 22B/8rec
        rep = analyze(g, {"S": 4, "D": 2})
        self.assertEqual(rep.total.bytes, 22)
        self.assertEqual(rep.total.records, 8)

        # 只提供 S，缺少 D：records/bytes 都被递归维度拖成 None
        rep_missing = analyze(g, {"S": 4})
        self.assertIsNone(rep_missing.total.bytes)
        self.assertIsNone(rep_missing.total.records)
        verdict_ids = {v.branch_id for v in rep_missing.rejectable_branches}
        self.assertIn("rec", verdict_ids)

        # 拒绝 rec 区域后，seg 的有限上界保留
        rep2 = rep_missing.reject_branches("rec")
        self.assertEqual(rep2.total.bytes, 12)
        self.assertEqual(rep2.total.records, 4)

    def test_nested_exclusive_loops_do_not_cross_add(self):
        # 分支两臂各自包含“分段 × 递归”，验证 max 在多层缩放后仍正确
        g = {"op": "branch", "arms": [
            {"name": "a", "node": {
                "op": "segments", "count": 2,
                "body": {"op": "recurse", "depth": 2, "fan_out": 2,
                          # 每 segment: 3 实例 × 4B = 12B；×2 seg = 24B
                          "body": {"op": "alloc", "bytes": 4, "records": 1}}}},
            {"name": "b", "node": {
                "op": "segments", "count": 1,
                "body": {"op": "recurse", "depth": 4, "fan_out": 1,
                          # 4 层 × 6B = 24B；×1 = 24B
                          "body": {"op": "alloc", "bytes": 6, "records": 1}}}},
        ]}
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 24)
        # a records: 2 seg × 3 instances = 6 ; b: 1 × 4 = 4 -> max 6
        self.assertEqual(rep.total.records, 6)

    def test_reject_one_entire_strategy_arm(self):
        rep = analyze(grand_graph())
        arm_id = "strategy#arm:bushy"
        rep2 = rep.reject_branches(arm_id)
        # 只剩 linear：64 + 30 + 8 = 102B ; 2 + 3 + 1 = 6rec
        self.assertEqual(rep2.total.bytes, 102)
        self.assertEqual(rep2.total.records, 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
