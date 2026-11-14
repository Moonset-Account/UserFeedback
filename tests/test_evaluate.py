"""核心判定规则单元测试：每条规则对应需求中的一项明确要求。"""

import copy
import unittest

from aggregator.evaluate import (
    evaluate,
    STATUS_CLOSABLE,
    STATUS_PENDING_DEPLOY,
    STATUS_PENDING_VERIFY,
)


def base_data():
    """构造一个'可关闭'的基准输入，各测试在此基础上做单一变量修改。"""
    return {
        "fix": {
            "ticket": "SEC-TEST-001",
            "fixed_at": "2026-11-10T08:00:00Z",
            "components": [{"name": "svc", "fixed_artifact": "svc:2.0:b100"}],
        },
        "deployments": {
            "instances": [
                {"instance_id": "i-1", "component": "svc", "environment": "prod",
                 "artifact": "svc:2.0:b100", "machine_id": "m-1",
                 "state": "active", "commissioned_at": "2026-11-09T09:00:00Z"},
            ]
        },
        "regression": {
            "results": [
                {"check_id": "CHK-1", "component": "svc", "env_category": "prod",
                 "artifact": "svc:1.0:b90", "phase": "pre_fix", "result": "pass",
                 "run_at": "2026-11-09T10:00:00Z"},
                {"check_id": "CHK-1", "component": "svc", "env_category": "prod",
                 "artifact": "svc:2.0:b100", "phase": "post_fix", "result": "pass",
                 "run_at": "2026-11-11T10:00:00Z"},
            ]
        },
        "machines": {
            "machines": [
                {"machine_id": "m-1", "hostname": "h-1", "environment": "prod",
                 "status": "online"},
            ]
        },
    }


def only_decision(report):
    assert len(report["decisions"]) == 1, report["decisions"]
    return report["decisions"][0]


class TestStatusRules(unittest.TestCase):

    def test_closable_when_deployed_and_paired(self):
        decision = only_decision(evaluate(base_data()))
        self.assertEqual(decision["status"], STATUS_CLOSABLE)
        self.assertEqual(decision["status_label"], "可关闭")
        # 证据链必须以结论收尾，且包含修复/部署/回归三类证据
        kinds = [e["type"] for e in decision["evidence_chain"]]
        self.assertEqual(kinds[-1], "conclusion")
        for required in ("fix", "deployment", "regression"):
            self.assertIn(required, kinds)

    def test_pending_deploy_when_instance_runs_old_artifact(self):
        data = base_data()
        data["deployments"]["instances"][0]["artifact"] = "svc:1.0:b90"
        decision = only_decision(evaluate(data))
        self.assertEqual(decision["status"], STATUS_PENDING_DEPLOY)
        self.assertEqual(decision["status_label"], "待部署")

    def test_pending_deploy_when_tests_only_cover_candidate(self):
        """测试通过但只跑了候选产物 => 继续显示待部署（只认实际部署的产物身份）。"""
        data = base_data()
        data["regression"]["results"][1]["artifact"] = "svc:2.0-rc1:b99"
        decision = only_decision(evaluate(data))
        self.assertEqual(decision["status"], STATUS_PENDING_DEPLOY)
        kinds = [e["type"] for e in decision["evidence_chain"]]
        self.assertIn("candidate_only", kinds)

    def test_pending_verify_when_post_missing(self):
        """修复前结果无法与同检查同环境的修复后结果配对 => 待验证并列出缺项。"""
        data = base_data()
        data["regression"]["results"] = data["regression"]["results"][:1]
        decision = only_decision(evaluate(data))
        self.assertEqual(decision["status"], STATUS_PENDING_VERIFY)
        self.assertEqual(len(decision["missing"]), 1)
        self.assertIn("CHK-1", decision["missing"][0])
        self.assertIn("post_fix", decision["missing"][0])

    def test_pending_verify_when_pre_baseline_missing(self):
        data = base_data()
        data["regression"]["results"] = data["regression"]["results"][1:]
        decision = only_decision(evaluate(data))
        self.assertEqual(decision["status"], STATUS_PENDING_VERIFY)
        self.assertEqual(len(decision["missing"]), 1)
        self.assertIn("pre_fix", decision["missing"][0])

    def test_pending_verify_when_env_category_differs(self):
        """修复后结果来自不同环境类别 => 不算配对。"""
        data = base_data()
        data["regression"]["results"][1]["env_category"] = "staging"
        report = evaluate(data)
        by_env = {d["environment"]: d for d in report["decisions"]}
        self.assertEqual(by_env["prod"]["status"], STATUS_PENDING_VERIFY)
        self.assertEqual(len(by_env["prod"]["missing"]), 1)
        # staging 侧只有单边结果，同样缺项
        self.assertEqual(by_env["staging"]["status"], STATUS_PENDING_VERIFY)

    def test_pending_verify_when_regression_fails(self):
        data = base_data()
        data["regression"]["results"][1]["result"] = "fail"
        decision = only_decision(evaluate(data))
        self.assertEqual(decision["status"], STATUS_PENDING_VERIFY)

    def test_decommissioned_counted_separately(self):
        """已下线实例的证据与修复完成分开统计，不影响关闭结论。"""
        data = base_data()
        data["deployments"]["instances"].append(
            {"instance_id": "i-9", "component": "svc", "environment": "prod",
             "artifact": "svc:1.0:b90", "machine_id": "m-9",
             "state": "decommissioned", "commissioned_at": "2026-10-01T00:00:00Z",
             "decommissioned_at": "2026-11-10T07:00:00Z"})
        data["machines"]["machines"].append(
            {"machine_id": "m-9", "hostname": "h-9", "environment": "prod",
             "status": "decommissioned"})
        decision = only_decision(evaluate(data))
        self.assertEqual(decision["status"], STATUS_CLOSABLE)
        self.assertEqual(decision["summary"]["decommissioned_instances"], 1)
        self.assertEqual(decision["decommissioned_instances"][0]["instance_id"], "i-9")
        kinds = [e["type"] for e in decision["evidence_chain"]]
        self.assertIn("decommissioned", kinds)

    def test_all_decommissioned_env_is_closable(self):
        data = base_data()
        data["deployments"]["instances"][0]["state"] = "decommissioned"
        data["deployments"]["instances"][0]["decommissioned_at"] = "2026-11-10T07:00:00Z"
        decision = only_decision(evaluate(data))
        self.assertEqual(decision["status"], STATUS_CLOSABLE)
        self.assertEqual(decision["summary"]["active_instances"], 0)

    def test_new_instance_without_covering_evidence_is_pending_verify(self):
        """修复后新上线的实例，回归证据早于其上线时间 => 待验证。"""
        data = base_data()
        data["deployments"]["instances"].append(
            {"instance_id": "i-2", "component": "svc", "environment": "prod",
             "artifact": "svc:2.0:b100", "machine_id": "m-2",
             "state": "active", "commissioned_at": "2026-11-12T02:00:00Z"})
        data["machines"]["machines"].append(
            {"machine_id": "m-2", "hostname": "h-2", "environment": "prod",
             "status": "online"})
        decision = only_decision(evaluate(data))
        self.assertEqual(decision["status"], STATUS_PENDING_VERIFY)
        self.assertEqual(decision["summary"]["new_instances"], 1)
        self.assertFalse(decision["new_instances"][0]["verified"])
        self.assertTrue(any("i-2" in m for m in decision["missing"]))

    def test_new_instance_covered_by_later_evidence_is_closable(self):
        """新实例上线时间早于修复后回归 => 证据覆盖，可关闭。"""
        data = base_data()
        data["deployments"]["instances"].append(
            {"instance_id": "i-2", "component": "svc", "environment": "prod",
             "artifact": "svc:2.0:b100", "machine_id": "m-2",
             "state": "active", "commissioned_at": "2026-11-11T08:00:00Z"})
        data["machines"]["machines"].append(
            {"machine_id": "m-2", "hostname": "h-2", "environment": "prod",
             "status": "online"})
        decision = only_decision(evaluate(data))
        self.assertEqual(decision["status"], STATUS_CLOSABLE)
        self.assertTrue(decision["new_instances"][0]["verified"])

    def test_instance_on_decommissioned_machine_treated_as_decommissioned(self):
        data = base_data()
        data["machines"]["machines"][0]["status"] = "decommissioned"
        decision = only_decision(evaluate(data))
        self.assertEqual(decision["summary"]["active_instances"], 0)
        self.assertEqual(decision["summary"]["decommissioned_instances"], 1)
        self.assertEqual(decision["status"], STATUS_CLOSABLE)

    def test_instance_with_unknown_machine_is_unverifiable(self):
        data = base_data()
        data["machines"]["machines"] = []
        report = evaluate(data)
        decision = only_decision(report)
        self.assertEqual(decision["status"], STATUS_PENDING_VERIFY)
        self.assertEqual(decision["summary"]["unverifiable_instances"], 1)
        self.assertTrue(report["warnings"])

    def test_evidence_chain_allows_review(self):
        """每条结论必须附可复核证据链：非空、含结论条目、每条含 type/ref/detail。"""
        decision = only_decision(evaluate(base_data()))
        chain = decision["evidence_chain"]
        self.assertGreaterEqual(len(chain), 4)
        for entry in chain:
            self.assertIn("type", entry)
            self.assertIn("ref", entry)
            self.assertIn("detail", entry)
        self.assertEqual(chain[-1]["type"], "conclusion")


if __name__ == "__main__":
    unittest.main()
