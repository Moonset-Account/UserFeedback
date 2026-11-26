"""分段展开、外部证据上限、子包递归、命名调用与调用环。"""

import sys
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from budget_guard import analyze  # noqa: E402
from budget_guard.model import B  # noqa: E402


class TestSegments(unittest.TestCase):
    def test_literal_count_multiplies(self):
        g = {"op": "segments", "count": 3,
             "body": {"op": "alloc", "bytes": 10, "records": 2}}
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 30)
        self.assertEqual(rep.total.records, 6)

    def test_evidence_count(self):
        g = {"op": "segments", "id": "seg",
             "count": {"evidence": "max_segments"},
             "body": {"op": "alloc", "bytes": 8, "records": 1}}
        rep = analyze(g, {"max_segments": 5})
        self.assertEqual(rep.total.bytes, 40)
        self.assertEqual(rep.total.records, 5)

    def test_evidence_object_form_with_source(self):
        g = {"op": "segments",
             "count": {"evidence": "max_segments"},
             "body": {"op": "alloc", "bytes": 1, "records": 0}}
        rep = analyze(g, {"max_segments": {
            "limit": 12, "source": "signed-manifest#segments"}})
        self.assertEqual(rep.total.bytes, 12)

    def test_missing_evidence_is_blocking_with_position(self):
        g = {"op": "segments", "id": "seg", "pos": "meta#line77",
             "count": {"evidence": "max_segments"},
             "body": {"op": "alloc", "bytes": 8}}
        rep = analyze(g, {})
        self.assertIsNone(rep.total.bytes)
        iss = rep.blocking_issues[0]
        self.assertEqual(iss.code, B.UNBOUNDED_COUNT)
        self.assertEqual(iss.evidence_key, "max_segments")
        self.assertEqual(iss.pos, "meta#line77")
        self.assertEqual(rep.rejectable_branches[0].branch_id, "seg")

    def test_invalid_evidence_value(self):
        g = {"op": "segments",
             "count": {"evidence": "max_segments"},
             "body": {"op": "alloc", "bytes": 8}}
        rep = analyze(g, {"max_segments": -4})
        self.assertTrue(any(i.code == B.INVALID_EVIDENCE
                            for i in rep.blocking_issues))
        self.assertIsNone(rep.total.bytes)

    def test_zero_count_makes_body_unreachable_but_keeps_reason(self):
        # count=0：段体里即使引用缺失证据，也不可达；成本为 0，
        # 原始原因保留为非活动 Issue。
        g = {"op": "segments", "id": "zero-seg", "count": 0,
             "body": {"op": "segments", "id": "inner",
                      "count": {"evidence": "missing"},
                      "body": {"op": "alloc", "bytes": 1}}}
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 0)
        self.assertEqual(rep.total.records, 0)
        self.assertEqual(rep.blocking_issues, [])
        all_codes = [i.code for i in rep.issues]
        self.assertIn(B.UNBOUNDED_COUNT, all_codes)
        self.assertTrue(all(not i.active for i in rep.issues))

    def test_unknown_count_zero_memory_but_positive_records(self):
        # 维度独立：段体 bytes=0 时，即使次数无证据，内存上界仍可证明为 0；
        # records 为正 -> None。
        g = {"op": "segments",
             "count": {"evidence": "n"},
             "body": {"op": "alloc", "bytes": 0, "records": 2}}
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 0)
        self.assertIsNone(rep.total.records)

    def test_huge_evidence_uses_closed_form_exact_product(self):
        g = {"op": "segments",
             "count": {"evidence": "n"},
             "body": {"op": "alloc", "bytes": 10**9, "records": 1}}
        rep = analyze(g, {"n": 10**12})
        self.assertEqual(rep.total.bytes, 10**21)
        self.assertEqual(rep.total.records, 10**12)


class TestRecursion(unittest.TestCase):
    def test_linear_recursion_default_fanout(self):
        g = {"op": "recurse", "id": "pkg", "depth": 3,
             "body": {"op": "alloc", "bytes": 7, "records": 1}}
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 21)
        self.assertEqual(rep.total.records, 3)

    def test_depth_from_evidence(self):
        g = {"op": "recurse",
             "depth": {"evidence": "max_depth"},
             "body": {"op": "alloc", "bytes": 2, "records": 5}}
        rep = analyze(g, {"max_depth": 4})
        self.assertEqual(rep.total.bytes, 8)
        self.assertEqual(rep.total.records, 20)

    def test_missing_depth_evidence_blocks(self):
        g = {"op": "recurse", "id": "r", "pos": "pkg.meta#recurse",
             "depth": {"evidence": "max_depth"},
             "body": {"op": "alloc", "bytes": 2}}
        rep = analyze(g)
        self.assertIsNone(rep.total.bytes)
        iss = rep.blocking_issues[0]
        self.assertEqual(iss.code, B.UNBOUNDED_RECURSION)
        self.assertEqual(iss.pos, "pkg.meta#recurse")

    def test_geometric_fanout_bound(self):
        # depth=4, fan_out=2 -> 15 个包实例
        g = {"op": "recurse", "depth": 4, "fan_out": 2,
             "body": {"op": "alloc", "bytes": 10, "records": 2}}
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 150)
        self.assertEqual(rep.total.records, 30)

    def test_depth_one_unknown_fanout_is_still_bounded(self):
        g = {"op": "recurse", "id": "r1", "depth": 1,
             "fan_out": {"evidence": "fan"},
             "body": {"op": "alloc", "bytes": 11, "records": 1}}
        rep = analyze(g, {})  # fan 证据缺失
        self.assertEqual(rep.total.bytes, 11)
        self.assertEqual(rep.total.records, 1)
        # 原因保留但不活动、不阻断
        self.assertTrue(any(i.code == B.UNBOUNDED_RECURSION for i in rep.issues))
        self.assertEqual(rep.blocking_issues, [])

    def test_missing_fanout_with_depth_gt_one_blocks(self):
        g = {"op": "recurse", "depth": 3,
             "fan_out": {"evidence": "fan"},
             "body": {"op": "alloc", "bytes": 11}}
        rep = analyze(g, {})
        self.assertIsNone(rep.total.bytes)
        self.assertTrue(any(i.code == B.UNBOUNDED_RECURSION
                            for i in rep.blocking_issues))

    def test_zero_depth_zero_cost_and_muted_body(self):
        g = {"op": "recurse", "depth": 0,
             "body": {"op": "alloc", "bytes": 11}}
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 0)
        self.assertTrue(rep.root.children[0].unreachable)

    def test_zero_depth_mutes_unknown_target(self):
        g = {"root": {"op": "recurse", "depth": 0, "target": "ghost"},
             "definitions": {}}
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 0)
        self.assertEqual(rep.blocking_issues, [])
        self.assertTrue(any(i.code == B.UNKNOWN_TARGET for i in rep.issues))
        self.assertTrue(all(not i.active for i in rep.issues))

    def test_recurse_named_target(self):
        g = {
            "root": {"op": "recurse", "depth": 2, "target": "pkg"},
            "definitions": {
                "pkg": {"op": "alloc", "bytes": 9, "records": 1}
            },
        }
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 18)
        self.assertEqual(rep.total.records, 2)

    def test_recurse_unknown_target(self):
        g = {"root": {"op": "recurse", "depth": 2, "target": "ghost"},
             "definitions": {}}
        rep = analyze(g)
        self.assertTrue(any(i.code == B.UNKNOWN_TARGET
                            for i in rep.blocking_issues))
        self.assertIsNone(rep.total.bytes)


class TestCallsAndCycles(unittest.TestCase):
    def test_two_calls_in_seq_count_twice(self):
        # 两次真实调用是两次展开，成本必须相加（命名定义不代表成本共享）
        g = {
            "root": {"op": "seq", "steps": [
                {"op": "call", "target": "pkg"},
                {"op": "call", "target": "pkg"},
            ]},
            "definitions": {"pkg": {"op": "alloc", "bytes": 15, "records": 1}},
        }
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 30)
        self.assertEqual(rep.total.records, 2)

    def test_call_inherits_sharing_environment(self):
        # 调用点之前分配并注册的对象，在被调用子包中同样证明共享
        g = {
            "root": {"op": "seq", "steps": [
                {"op": "alloc", "bytes": 100, "share": "hdr"},
                {"op": "call", "target": "pkg"},
            ]},
            "definitions": {"pkg": {
                "op": "ref", "share": "hdr", "bytes": 100}},
        }
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 100)
        self.assertEqual(rep.warnings, [])

    def test_unknown_call_target(self):
        g = {"op": "call", "id": "c", "target": "missing",
             "pos": "meta#call-missing"}
        rep = analyze(g)
        iss = rep.blocking_issues[0]
        self.assertEqual(iss.code, B.UNKNOWN_TARGET)
        self.assertEqual(iss.pos, "meta#call-missing")
        self.assertIsNone(rep.total.bytes)

    def test_static_recursion_cycle_is_unbounded(self):
        g = {
            "root": {"op": "call", "target": "a", "id": "root-call"},
            "definitions": {
                "a": {"op": "call", "id": "a-to-b", "target": "b",
                      "pos": "a.json#3"},
                "b": {"op": "call", "id": "b-to-a", "target": "a",
                      "pos": "b.json#7"},
            },
        }
        rep = analyze(g)
        codes = {i.code for i in rep.blocking_issues}
        self.assertIn(B.RECURSION_CYCLE, codes)
        cycle_issue = next(i for i in rep.blocking_issues
                          if i.code == B.RECURSION_CYCLE)
        self.assertEqual(cycle_issue.pos, "b.json#7")
        self.assertIn("a -> b -> a", cycle_issue.message)
        # 拒绝环上的 b-to-a 节点后，环被切断，成本可证（a->b 无进一步调用）
        rep2 = analyze(g, reject=["b-to-a"])
        self.assertEqual(rep2.total.bytes, 0)
        self.assertEqual(rep2.blocking_issues, [])

    def test_self_recursion_cycle(self):
        g = {
            "root": {"op": "call", "target": "loop"},
            "definitions": {"loop": {
                "op": "seq", "steps": [
                    {"op": "alloc", "bytes": 3},
                    {"op": "call", "id": "self-call", "target": "loop"},
                ]}},
        }
        rep = analyze(g)
        self.assertTrue(any(i.code == B.RECURSION_CYCLE
                            for i in rep.blocking_issues))
        self.assertIsNone(rep.total.bytes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
