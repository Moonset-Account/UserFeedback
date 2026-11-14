"""字段血缘分级单元测试。

每个用例对应一个稳定 case_id（见各测试方法 docstring），
覆盖原始需求中的明确行为：最高级继承、降级规则命中与批准范围匹配、
血缘缺边待核对、环迭代稳定、撤销批准后的下游影响、逐字段解释。
"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from lineage import (  # noqa: E402
    ManifestError,
    classify,
    diff_reports,
    revoke_rule,
    validate_manifest,
)

LEVELS = ["S1", "S2", "S3", "S4"]


def make_manifest(**kw):
    base = {"levels": LEVELS, "fields": {}, "edges": [], "downgrades": []}
    base.update(kw)
    return validate_manifest(base)


class TestLineageClassification(unittest.TestCase):
    def test_case_max_inherit(self):
        """CASE-MAX-INHERIT: 一般派生取输入中的最高等级。"""
        m = make_manifest(
            fields={"a.x": {"level": "S2"}, "a.y": {"level": "S4"}},
            edges=[{"from": ["a.x", "a.y"], "to": "b.z"}],
        )
        report = classify(m)
        field = report["fields"]["b.z"]
        self.assertEqual(field["level"], "S4")
        self.assertEqual(field["status"], "ok")
        self.assertIn("a.y", field["explanation"])
        self.assertIn("S4", field["explanation"])

    def test_case_downgrade_applied(self):
        """CASE-DG-APPLIED: 批准范围匹配时按规则降到目标级别。"""
        m = make_manifest(
            fields={"a.x": {"level": "S4"}},
            edges=[{"from": ["a.x"], "to": "b.z"}],
            downgrades=[
                {
                    "id": "DG-1",
                    "output": "b.z",
                    "inputs": {"a.x": "S4"},
                    "target": "S1",
                    "status": "approved",
                }
            ],
        )
        report = classify(m)
        field = report["fields"]["b.z"]
        self.assertEqual(field["level"], "S1")
        self.assertIn("DG-1", field["explanation"])
        self.assertEqual(report["downgrades"]["applied"], ["DG-1"])
        self.assertEqual(report["downgrades"]["unmatched"], [])

    def test_case_downgrade_scope_must_match(self):
        """CASE-DG-SCOPE: 批准范围（输出字段+输入等级）不匹配时规则不生效。"""
        m = make_manifest(
            fields={"a.x": {"level": "S2"}},
            edges=[{"from": ["a.x"], "to": "b.z"}],
            downgrades=[
                {
                    "id": "DG-BAD-LEVEL",
                    "output": "b.z",
                    "inputs": {"a.x": "S3"},  # 实际输入是 S2，不匹配
                    "target": "S1",
                    "status": "approved",
                },
                {
                    "id": "DG-BAD-OUTPUT",
                    "output": "b.other",  # 输出字段无血缘边
                    "inputs": {"a.x": "S2"},
                    "target": "S1",
                    "status": "approved",
                },
            ],
        )
        report = classify(m)
        # 两条规则都不得生效，b.z 保持输入最高等级 S2
        self.assertEqual(report["fields"]["b.z"]["level"], "S2")
        self.assertEqual(report["downgrades"]["applied"], [])
        unmatched_ids = {u["id"] for u in report["downgrades"]["unmatched"]}
        self.assertEqual(unmatched_ids, {"DG-BAD-LEVEL", "DG-BAD-OUTPUT"})
        reasons = {u["id"]: u["reason"] for u in report["downgrades"]["unmatched"]}
        self.assertIn("a.x", reasons["DG-BAD-LEVEL"])
        self.assertIn("S3", reasons["DG-BAD-LEVEL"])
        self.assertIn("无血缘边", reasons["DG-BAD-OUTPUT"])

    def test_case_missing_edge_pending(self):
        """CASE-PENDING-MISSING-EDGE: 血缘缺边且无原始等级的字段标记待核对。"""
        m = make_manifest(
            fields={"a.x": {"level": "S2"}},
            edges=[{"from": ["a.x", "ext.ghost"], "to": "b.z"}],
        )
        report = classify(m)
        ghost = report["fields"]["ext.ghost"]
        self.assertEqual(ghost["status"], "pending_review")
        self.assertIsNone(ghost["level"])
        self.assertIn("血缘缺边", ghost["explanation"])
        self.assertIn("ext.ghost", report["pending_review"])

    def test_case_pending_propagates(self):
        """CASE-PENDING-PROPAGATION: 输入待核对时，下游字段一并待核对。"""
        m = make_manifest(edges=[{"from": ["ext.ghost"], "to": "b.z"}])
        report = classify(m)
        downstream = report["fields"]["b.z"]
        self.assertEqual(downstream["status"], "pending_review")
        self.assertIn("ext.ghost", downstream["explanation"])
        self.assertEqual(
            report["pending_review"], ["b.z", "ext.ghost"]
        )

    def test_case_cycle_converges(self):
        """CASE-CYCLE-STABLE: 环中的等级迭代到稳定，并报告涉及哪些字段。"""
        m = make_manifest(
            fields={"a.x": {"level": "S3"}},
            edges=[
                {"from": ["a.x", "c.q"], "to": "c.p"},
                {"from": ["c.p"], "to": "c.q"},
            ],
        )
        report = classify(m)
        self.assertEqual(report["fields"]["c.p"]["level"], "S3")
        self.assertEqual(report["fields"]["c.q"]["level"], "S3")
        self.assertEqual(report["cycles"], [["c.p", "c.q"]])
        self.assertTrue(report["fields"]["c.p"]["in_cycle"])
        self.assertTrue(report["fields"]["c.q"]["in_cycle"])
        self.assertFalse(report["fields"]["a.x"]["in_cycle"])

    def test_case_revoke_lists_downstream(self):
        """CASE-REVOKE-IMPACT: 撤销批准后重新载入，列出受影响的下游字段。"""
        m = make_manifest(
            fields={"a.ssn": {"level": "S4"}},
            edges=[
                {"from": ["a.ssn"], "to": "b.profile"},
                {"from": ["b.profile"], "to": "c.mart"},
                {"from": ["c.mart"], "to": "d.report"},
            ],
            downgrades=[
                {
                    "id": "DG-1",
                    "output": "b.profile",
                    "inputs": {"a.ssn": "S4"},
                    "target": "S1",
                    "status": "approved",
                }
            ],
        )
        before = classify(m)
        self.assertEqual(before["fields"]["d.report"]["level"], "S1")

        updated = revoke_rule(m, "DG-1")
        after = classify(updated)
        affected = diff_reports(before, after)

        names = [a["field"] for a in affected]
        self.assertEqual(names, ["b.profile", "c.mart", "d.report"])
        for item in affected:
            self.assertEqual(item["before"]["level"], "S1")
            self.assertEqual(item["after"]["level"], "S4")
        # 原始字段不受影响
        self.assertNotIn("a.ssn", names)
        # 撤销信息出现在报告里
        self.assertEqual(after["downgrades"]["revoked"], ["DG-1"])

    def test_case_explanation_present(self):
        """CASE-EXPLANATION: 每个输出字段都带一条非空解释来源。"""
        m = make_manifest(
            fields={"a.x": {"level": "S2"}},
            edges=[{"from": ["a.x"], "to": "b.z"}],
        )
        report = classify(m)
        self.assertGreaterEqual(len(report["fields"]), 2)
        for name, info in report["fields"].items():
            self.assertIsInstance(info["explanation"], str)
            self.assertTrue(info["explanation"].strip(), "字段 %s 缺少解释" % name)

    def test_case_manifest_validation(self):
        """CASE-MANIFEST-VALIDATION: 非法清单被拒绝并给出原因。"""
        with self.assertRaises(ManifestError):
            validate_manifest({"fields": {"a.x": {"level": "S9"}}})
        with self.assertRaises(ManifestError):
            validate_manifest({"edges": [{"from": [], "to": "b.z"}]})
        with self.assertRaises(ManifestError):
            validate_manifest(
                {
                    "downgrades": [
                        {"id": "DG-1", "output": "b.z", "inputs": {"a.x": "S2"},
                         "target": "S1"},
                        {"id": "DG-1", "output": "b.y", "inputs": {"a.x": "S2"},
                         "target": "S1"},
                    ]
                }
            )
        with self.assertRaises(ManifestError):
            revoke_rule(make_manifest(), "DG-NOT-EXIST")


class TestSampleManifestEndToEnd(unittest.TestCase):
    def test_case_sample_e2e(self):
        """CASE-SAMPLE-E2E: 通过 CLI 对交付样例做端到端分级与撤销。"""
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "app.py"),
             "classify", "samples/manifest.json"],
            capture_output=True, text=True, cwd=ROOT, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)

        self.assertEqual(
            report["fields"]["mart.customer_profile"]["level"], "S2"
        )
        self.assertEqual(report["downgrades"]["applied"], ["DG-1"])
        unmatched_ids = {u["id"] for u in report["downgrades"]["unmatched"]}
        self.assertEqual(unmatched_ids, {"DG-2"})
        self.assertEqual(
            report["fields"]["mart.mystery"]["status"], "pending_review"
        )
        self.assertEqual(report["cycles"], [["feed.p", "feed.q"]])

        proc2 = subprocess.run(
            [sys.executable, os.path.join(ROOT, "app.py"),
             "revoke", "samples/manifest.json", "DG-1"],
            capture_output=True, text=True, cwd=ROOT, timeout=60,
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        result = json.loads(proc2.stdout)
        self.assertEqual(result["revoked_rule"], "DG-1")
        names = [a["field"] for a in result["affected_fields"]]
        self.assertEqual(names, ["mart.customer_profile", "mart.vip_list"])


if __name__ == "__main__":
    unittest.main()
