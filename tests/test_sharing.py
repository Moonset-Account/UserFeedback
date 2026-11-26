"""共享判定：支配性分配、匿名引用保守新分配、分支交集、条件复用。"""

import sys
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from budget_guard import analyze  # noqa: E402
from budget_guard.model import B  # noqa: E402


class TestProvenReferences(unittest.TestCase):
    def test_dominating_named_alloc_zeroes_ref(self):
        g = {"op": "seq", "steps": [
            {"op": "alloc", "bytes": 4096, "records": 1, "share": "dict"},
            {"op": "ref", "share": "dict", "bytes": 4096},
        ]}
        rep = analyze(g)
        # 只有第一次分配计入内存；ref 默认 reuse_records=0
        self.assertEqual(rep.total.bytes, 4096)
        self.assertEqual(rep.total.records, 1)
        self.assertEqual(rep.warnings, [])

    def test_proven_ref_can_log_reuse_records(self):
        g = {"op": "seq", "steps": [
            {"op": "alloc", "bytes": 10, "share": "d"},
            {"op": "ref", "share": "d", "reuse_records": 2},
        ]}
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 10)
        self.assertEqual(rep.total.records, 1 + 2)

    def test_share_marker_op(self):
        g = {"op": "seq", "steps": [
            {"op": "alloc", "bytes": 10},
            {"op": "share", "key": "d"},
            {"op": "ref", "share": "d", "bytes": 10},
        ]}
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 10)


class TestAnonymousAndUnproven(unittest.TestCase):
    def test_anonymous_ref_is_fresh_allocation(self):
        # 题面核心陷阱：匿名引用不能假定共享
        g = {"op": "seq", "steps": [
            {"op": "alloc", "bytes": 100, "share": "known"},
            {"op": "ref", "bytes": 77, "pos": "meta#anon"},
        ]}
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 177)
        self.assertEqual(len(rep.warnings), 1)
        self.assertEqual(rep.warnings[0].code, B.ANONYMOUS_REFERENCE)
        self.assertEqual(rep.warnings[0].pos, "meta#anon")

    def test_anonymous_ref_without_size_is_blocking(self):
        g = {"op": "ref", "id": "anon-no-size"}
        rep = analyze(g)
        self.assertIsNone(rep.total.bytes)
        self.assertTrue(any(i.code == B.MISSING_OBJECT_SIZE
                            for i in rep.blocking_issues))
        self.assertEqual(
            rep.rejectable_branches[0].branch_id, "anon-no-size")

    def test_named_ref_use_before_def_is_fresh(self):
        g = {"op": "seq", "steps": [
            {"op": "ref", "share": "later", "bytes": 40},
            {"op": "alloc", "bytes": 40, "share": "later"},
        ]}
        rep = analyze(g)
        # ref 在前，不能证明共享 -> 40 + 40
        self.assertEqual(rep.total.bytes, 80)
        self.assertTrue(any(i.code == B.UNPROVEN_SHARING
                            for i in rep.warnings))

    def test_named_ref_unknown_key_without_size_blocks(self):
        g = {"op": "ref", "share": "ghost"}
        rep = analyze(g)
        codes = {i.code for i in rep.blocking_issues}
        self.assertIn(B.MISSING_OBJECT_SIZE, codes)
        # 同时保留“未证明共享”的警告
        self.assertTrue(any(i.code == B.UNPROVEN_SHARING
                            for i in rep.warnings))

    def test_anonymous_refs_in_exclusive_arms_count_once(self):
        # 两个互斥臂各有一个匿名引用：max 保证不会算成两次
        g = {"op": "branch", "arms": [
            {"name": "a", "node": {"op": "ref", "bytes": 50}},
            {"name": "b", "node": {"op": "ref", "bytes": 80}},
        ]}
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 80)
        self.assertEqual(len(rep.warnings), 2)  # 两条警告都保留


class TestDominanceThroughBranchesAndLoops(unittest.TestCase):
    def test_share_inside_one_arm_not_visible_after_branch(self):
        g = {"op": "seq", "steps": [
            {"op": "branch", "arms": [
                {"name": "reg", "node": {
                    "op": "alloc", "bytes": 10, "share": "x"}},
                {"name": "plain", "node": {"op": "alloc", "bytes": 1}},
            ]},
            {"op": "ref", "share": "x", "bytes": 99},
        ]}
        rep = analyze(g)
        # 只有一臂注册 x -> 分支后不能证明共享 -> ref 保守 +99
        # branch max = max(10, 1) = 10；总计 10 + 99 = 109
        self.assertEqual(rep.total.bytes, 109)
        self.assertTrue(any(i.code == B.UNPROVEN_SHARING
                            for i in rep.warnings))

    def test_share_in_all_arms_visible_after_branch(self):
        g = {"op": "seq", "steps": [
            {"op": "branch", "arms": [
                {"name": "a", "node": {
                    "op": "alloc", "bytes": 10, "share": "x"}},
                {"name": "b", "node": {
                    "op": "alloc", "bytes": 12, "share": "x"}},
            ]},
            {"op": "ref", "share": "x", "bytes": 99},
        ]}
        rep = analyze(g)
        # 两臂都必定注册 x -> 分支后可证明共享
        self.assertEqual(rep.total.bytes, 12)  # max(10,12)，ref=0
        self.assertFalse(any(i.code == B.UNPROVEN_SHARING
                             for i in rep.warnings))

    def test_loop_does_not_export_share_because_count_is_upper_bound(self):
        # 证据给的是“上限”，实际可能为 0，因此循环后的引用不能据此证明共享
        g = {"op": "seq", "steps": [
            {"op": "segments", "count": 3, "body": {
                "op": "alloc", "bytes": 10, "share": "x"}},
            {"op": "ref", "share": "x", "bytes": 9},
        ]}
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 30 + 9)
        self.assertTrue(any(i.code == B.UNPROVEN_SHARING
                            for i in rep.warnings))

    def test_share_before_loop_proves_refs_inside_loop(self):
        g = {"op": "seq", "steps": [
            {"op": "alloc", "bytes": 64, "share": "hdr"},
            {"op": "segments", "count": 4, "body": {"op": "seq", "steps": [
                {"op": "ref", "share": "hdr", "bytes": 64},
                {"op": "alloc", "bytes": 5, "records": 1},
            ]}},
        ]}
        rep = analyze(g)
        # 64（唯一 header）+ 4*5；ref 全部 0
        self.assertEqual(rep.total.bytes, 84)
        # header alloc 1 条 + 4 个 segment alloc 各 1 条
        self.assertEqual(rep.total.records, 5)


class TestReuseIf(unittest.TestCase):
    def test_unproven_reuse_uses_fresh_bound_and_warns(self):
        g = {"op": "reuse_if", "id": "ru", "key": "cache",
             "bytes": 128, "records": 1}
        rep = analyze(g)
        # max(reuse=0, fresh=128/1) = fresh
        self.assertEqual(rep.total.bytes, 128)
        self.assertEqual(rep.total.records, 1)
        self.assertTrue(any(i.code == B.REUSE_WITHOUT_EVIDENCE
                            for i in rep.warnings))

    def test_proven_reuse_is_zero_and_mutes_fresh_arm(self):
        g = {"op": "seq", "steps": [
            {"op": "alloc", "bytes": 128, "share": "cache"},
            {"op": "reuse_if", "id": "ru", "key": "cache",
             "bytes": 128, "records": 1},
        ]}
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 128)
        self.assertEqual(rep.total.records, 1)
        fresh_arm = rep.find_region("ru#arm:fresh")
        self.assertIsNotNone(fresh_arm)
        self.assertTrue(all(not i.active for i in fresh_arm.issues)
                        if fresh_arm.issues else True)

    def test_reuse_without_fresh_cost_blocks(self):
        g = {"op": "reuse_if", "id": "ru", "key": "cache"}
        rep = analyze(g)
        self.assertIsNone(rep.total.bytes)
        self.assertTrue(any(i.code == B.MISSING_OBJECT_SIZE
                            for i in rep.blocking_issues))

    def test_structured_reuse_and_fresh_arms_take_max(self):
        g = {"op": "reuse_if", "id": "ru", "key": "cache",
             "reuse": {"op": "ref", "share": "cache", "reuse_records": 1},
             "fresh": {"op": "segments", "count": 2,
                       "body": {"op": "alloc", "bytes": 50, "records": 1}}}
        rep = analyze(g)
        # 未证明 cache -> max(reuse 0 bytes/1 rec, fresh 100 bytes/2 rec)
        self.assertEqual(rep.total.bytes, 100)
        self.assertEqual(rep.total.records, 2)

    def test_reject_fresh_arm_yields_zero_bound(self):
        g = {"op": "reuse_if", "id": "ru", "key": "cache",
             "bytes": {"evidence": "obj_size"}}
        rep = analyze(g)
        self.assertIsNone(rep.total.bytes)
        rep2 = rep.reject_branches("ru#arm:fresh")
        self.assertEqual(rep2.total.bytes, 0)
        self.assertEqual(rep2.blocking_issues, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
