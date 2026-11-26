"""互斥分支：必须取 max 而不是相加；问题定位与分支拒绝闭环。"""

import sys
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from budget_guard import analyze, evaluate  # noqa: E402
from budget_guard.model import B  # noqa: E402


class TestBranchMaxNotSum(unittest.TestCase):
    def test_componentwise_max(self):
        g = {"op": "branch", "id": "bw", "arms": [
            {"name": "a", "node": {"op": "alloc", "bytes": 100, "records": 1}},
            {"name": "b", "node": {"op": "alloc", "bytes": 200, "records": 5}},
        ]}
        rep = analyze(g)
        # max 维度分别取到不同臂的最大值：200 bytes / 5 records
        self.assertEqual(rep.total.bytes, 200)
        self.assertEqual(rep.total.records, 5)
        # 若是错误地相加，会得到 300 / 6
        self.assertNotEqual((rep.total.bytes, rep.total.records), (300, 6))

    def test_exclusive_expansions_are_not_added(self):
        # 题面核心陷阱：不同分支上互斥的展开不应相加
        g = {"op": "branch", "id": "mode", "pos": "meta#mode", "arms": [
            {"name": "many-small", "node": {
                "op": "segments", "id": "many", "count": 10,
                "body": {"op": "alloc", "bytes": 100, "records": 1}}},
            {"name": "few-large", "node": {
                "op": "segments", "id": "few", "count": 2,
                "body": {"op": "alloc", "bytes": 1000, "records": 4}}},
        ]}
        rep = analyze(g)
        # arm1 = 1000 bytes / 10 records
        # arm2 = 2000 bytes / 8 records
        # 互斥 max -> 2000 bytes / 10 records（绝不是相加的 3000 / 18）
        self.assertEqual(rep.total.bytes, 2000)
        self.assertEqual(rep.total.records, 10)

    def test_then_else_shorthand(self):
        g = {"op": "if", "condition": "fast-path",
             "then": {"op": "alloc", "bytes": 7, "records": 2},
             "else": {"op": "alloc", "bytes": 9, "records": 1}}
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 9)
        self.assertEqual(rep.total.records, 2)

    def test_three_arms(self):
        g = {"op": "choice", "cases": [
            {"op": "alloc", "bytes": 1, "records": 9},
            {"op": "alloc", "bytes": 5, "records": 1},
            {"op": "alloc", "bytes": 3, "records": 3},
        ]}
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 5)
        self.assertEqual(rep.total.records, 9)


class TestUnboundedArmAndRejection(unittest.TestCase):
    GRAPH = {
        "op": "branch", "id": "delivery", "pos": "archive.json#delivery",
        "arms": [
            {"name": "bounded", "node": {
                "op": "alloc", "id": "ok", "bytes": 120, "records": 2,
                "pos": "ops[0].ok"}},
            {"name": "evidence-heavy", "node": {
                "op": "segments", "id": "heavy", "pos": "ops[0].heavy",
                "count": {"evidence": "segment_limit"},
                "body": {"op": "alloc", "bytes": 64, "records": 1}}},
        ],
    }

    def test_missing_evidence_makes_branch_unprovable(self):
        rep = analyze(self.GRAPH, evidence={})
        self.assertIsNone(rep.total.bytes)
        self.assertIsNone(rep.total.records)
        codes = {i.code for i in rep.blocking_issues}
        self.assertIn(B.UNBOUNDED_COUNT, codes)
        verdict_ids = {v.branch_id for v in rep.rejectable_branches}
        self.assertIn("heavy", verdict_ids)
        heavy_v = next(v for v in rep.rejectable_branches if v.branch_id == "heavy")
        self.assertEqual(heavy_v.pos, "ops[0].heavy")
        self.assertTrue(any("segment_limit" in r for r in heavy_v.reasons))

    def test_gate_denies_until_branch_rejected(self):
        rep = analyze(self.GRAPH)
        d = evaluate(rep, max_bytes=10_000, max_records=10_000)
        self.assertFalse(d.allowed)
        self.assertTrue(d.blocking_issues)

        # 导入端按 verdict 给出的 branch_id 拒绝 heavy 节点后重算
        heavy_v = next(v for v in rep.rejectable_branches if v.branch_id == "heavy")
        rep2 = rep.reject_branches(heavy_v.branch_id)
        d2 = evaluate(rep2, max_bytes=10_000, max_records=10_000)
        self.assertTrue(d2.allowed, msg=str([i.message for i in d2.blocking_issues]))
        self.assertEqual(rep2.total.bytes, 120)
        self.assertEqual(rep2.total.records, 2)
        # 原始原因仍保留在报告中，只是不再活动；
        # 同时允许存在一条活动的 BRANCH_REJECTED 信息 Issue。
        retained = [i for i in rep2.issues
                    if i.region_id == "heavy" and i.code == B.UNBOUNDED_COUNT]
        self.assertTrue(retained)
        self.assertTrue(all(not i.active for i in retained))
        self.assertTrue(any(i.code == B.BRANCH_REJECTED and i.active
                            for i in rep2.issues))

    def test_reject_whole_arm_by_composite_id(self):
        # 没有显式 id 的臂可用 "<branch-id>#arm:<name>" 整体拒绝
        rep = analyze(self.GRAPH)
        arm_id = "delivery#arm:evidence-heavy"
        self.assertIn(arm_id, rep.known_region_ids)
        rep2 = analyze(self.GRAPH, reject=[arm_id])
        self.assertEqual(rep2.total.bytes, 120)
        self.assertEqual(rep2.total.records, 2)
        arm_report = rep2.find_region(arm_id)
        self.assertIsNotNone(arm_report)
        self.assertTrue(arm_report.rejected)

    def test_reject_bounded_arm_leaves_unbounded(self):
        rep2 = analyze(self.GRAPH, reject=["ok"])
        self.assertIsNone(rep2.total.bytes)
        self.assertTrue(rep2.blocking_issues)

    def test_unknown_reject_id_raises(self):
        with self.assertRaises(ValueError):
            analyze({"op": "alloc", "id": "a"}, reject=["does-not-exist"])

    def test_reject_root_zeroes_everything_but_keeps_reasons(self):
        g = {"op": "seq", "id": "whole", "steps": [
            {"op": "segments", "id": "s", "count": {"evidence": "x"},
             "body": {"op": "alloc", "bytes": 1}}
        ]}
        rep = analyze(g, reject=["whole"])
        self.assertEqual(rep.total.bytes, 0)
        self.assertEqual(rep.total.records, 0)
        # 原因仍在（非活动）
        self.assertTrue(any(i.code == B.UNBOUNDED_COUNT for i in rep.issues))
        self.assertEqual(rep.blocking_issues, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
