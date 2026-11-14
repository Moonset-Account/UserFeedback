"""密钥退役清点单元测试。

每个用例带稳定 case_id（RET-01 .. RET-15），断言消息中包含 case_id、
输入要点、实际输出与期望值，便于逐条对照需求。
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from retirement_core import InventoryError, build_report


def base_inventory():
    """最小合法清单：盘点时间 2026-01-05，firmware 保留 10 天。"""
    return {
        "inventory_time": "2026-01-05T00:00:00Z",
        "keys": [{"key_id": "K1", "signing_stopped_at": "2026-01-01T00:00:00Z"}],
        "retention_policies": [
            {"artifact_type": "firmware", "verify_retention_days": 10},
        ],
        "assets": [{"asset_id": "a1", "kind": "derived"}],
        "artifacts": [],
        "usage_edges": [],
        "reissues": [],
    }


def add_artifact(inv, artifact_id="p1", asset_id="a1", issued_at="2026-01-01T00:00:00Z",
                 key_id="K1", artifact_type="firmware"):
    inv["artifacts"].append({
        "artifact_id": artifact_id,
        "key_id": key_id,
        "asset_id": asset_id,
        "artifact_type": artifact_type,
        "issued_at": issued_at,
    })


def key_report(report, key_id="K1"):
    for entry in report["keys"]:
        if entry["key_id"] == key_id:
            return entry
    raise AssertionError(f"报告中缺少密钥 {key_id}")


class RetainedStockTests(unittest.TestCase):
    def test_ret01_unexpired_artifact_is_retained(self):
        case_id = "RET-01"
        inv = base_inventory()
        add_artifact(inv, issued_at="2026-01-01T00:00:00Z")  # 到期 2026-01-11
        rep = key_report(build_report(inv))
        actual = [(i["artifact_id"], i["retention_expires_at"]) for i in rep["retained_stock"]]
        expected = [("p1", "2026-01-11T00:00:00Z")]
        self.assertEqual(actual, expected,
                         f"{case_id}: 未到期产物应计入存量 expected={expected} actual={actual}")

    def test_ret02_expired_artifact_not_retained(self):
        case_id = "RET-02"
        inv = base_inventory()
        add_artifact(inv, issued_at="2025-12-20T00:00:00Z")  # 到期 2025-12-30 < 盘点时间
        rep = key_report(build_report(inv))
        actual = rep["retained_stock"]
        self.assertEqual(actual, [],
                         f"{case_id}: 已过保留期产物不计入存量 expected=[] actual={actual}")
        self.assertEqual(rep["status"], "retirable_now",
                         f"{case_id}: 无存量时应可退役 actual={rep['status']}")

    def test_ret03_expiry_equal_to_inventory_time_not_retained(self):
        case_id = "RET-03"
        inv = base_inventory()
        add_artifact(inv, issued_at="2025-12-26T00:00:00Z")  # 到期恰好 = 2026-01-05
        rep = key_report(build_report(inv))
        actual = rep["retained_stock"]
        self.assertEqual(actual, [],
                         f"{case_id}: 边界——保留期届满时刻即不再需验证 expected=[] actual={actual}")


class ReissueTests(unittest.TestCase):
    def test_ret04_completed_reissue_clears_only_that_version(self):
        case_id = "RET-04"
        inv = base_inventory()
        add_artifact(inv, artifact_id="p1")
        add_artifact(inv, artifact_id="p2")
        inv["reissues"].append({"artifact_id": "p1", "completed_at": "2026-01-02T00:00:00Z"})
        rep = key_report(build_report(inv))
        actual = [i["artifact_id"] for i in rep["retained_stock"]]
        expected = ["p2"]
        self.assertEqual(actual, expected,
                         f"{case_id}: 重新签发只解除对应产物版本 expected={expected} actual={actual}")

    def test_ret05_missing_completion_goes_to_manual_review(self):
        case_id = "RET-05"
        inv = base_inventory()
        add_artifact(inv, artifact_id="p1")
        inv["reissues"].append({"reissue_id": "rs1", "artifact_id": "p1",
                                "completed_at": None})
        rep = key_report(build_report(inv))
        reasons = [m["reason"] for m in rep["manual_review"]]
        self.assertEqual(reasons, ["reissue_completion_missing"],
                         f"{case_id}: 缺失完成时间应进人工清点 actual={reasons}")
        retained = [i["artifact_id"] for i in rep["retained_stock"]]
        self.assertEqual(retained, ["p1"],
                         f"{case_id}: 未完成的重新签发不解除依赖 actual={retained}")
        self.assertEqual(rep["status"], "manual_review",
                         f"{case_id}: actual={rep['status']}")
        self.assertIsNone(rep["earliest_safe_retirement"],
                          f"{case_id}: 人工清点期间不应给出退役时点 "
                          f"actual={rep['earliest_safe_retirement']}")

    def test_ret12_reissue_of_other_artifact_does_not_clear(self):
        case_id = "RET-12"
        inv = base_inventory()
        add_artifact(inv, artifact_id="p1")
        add_artifact(inv, artifact_id="p2", issued_at="2025-12-20T00:00:00Z")
        inv["reissues"].append({"artifact_id": "p2", "completed_at": "2026-01-02T00:00:00Z"})
        rep = key_report(build_report(inv))
        actual = [i["artifact_id"] for i in rep["retained_stock"]]
        expected = ["p1"]
        self.assertEqual(actual, expected,
                         f"{case_id}: 其他产物的重新签发不影响本产物 expected={expected} actual={actual}")

    def test_ret15_completed_reissue_makes_key_retirable(self):
        case_id = "RET-15"
        inv = base_inventory()
        add_artifact(inv, artifact_id="p1")
        inv["reissues"].append({"artifact_id": "p1", "completed_at": "2026-01-03T00:00:00Z"})
        rep = key_report(build_report(inv))
        self.assertEqual(rep["status"], "retirable_now",
                         f"{case_id}: 唯一产物已被重新签发解除 actual={rep['status']}")
        self.assertEqual(rep["earliest_safe_retirement"], "2026-01-05T00:00:00Z",
                         f"{case_id}: 可立即退役 actual={rep['earliest_safe_retirement']}")


class UnknownDestinationTests(unittest.TestCase):
    def test_ret06_unknown_destination_goes_to_manual_review(self):
        case_id = "RET-06"
        inv = base_inventory()
        add_artifact(inv)
        inv["usage_edges"].append({"edge_id": "e1", "from_asset": "a1",
                                   "to_asset": None, "destination_known": False})
        rep = key_report(build_report(inv))
        reasons = [m["reason"] for m in rep["manual_review"]]
        self.assertEqual(reasons, ["reference_destination_unknown"],
                         f"{case_id}: 引用去向不明应进人工清点 actual={reasons}")
        self.assertEqual(rep["status"], "manual_review",
                         f"{case_id}: actual={rep['status']}")

    def test_ret07_unknown_edge_on_unrelated_asset_ignored(self):
        case_id = "RET-07"
        inv = base_inventory()
        inv["assets"].append({"asset_id": "a2", "kind": "derived"})
        add_artifact(inv, asset_id="a1")
        inv["usage_edges"].append({"edge_id": "e1", "from_asset": "a2",
                                   "to_asset": None, "destination_known": False})
        rep = key_report(build_report(inv))
        actual = rep["manual_review"]
        self.assertEqual(actual, [],
                         f"{case_id}: 与本密钥无关的去向不明边不触发人工清点 expected=[] actual={actual}")


class EntryPointChainTests(unittest.TestCase):
    def test_ret08_active_entry_point_reported_with_source_chain(self):
        case_id = "RET-08"
        inv = base_inventory()
        inv["assets"] = [
            {"asset_id": "e1", "kind": "entry_point", "active": True},
            {"asset_id": "mid", "kind": "derived", "derives_from": "e1"},
            {"asset_id": "a1", "kind": "derived", "derives_from": "mid"},
        ]
        add_artifact(inv, asset_id="a1")
        rep = key_report(build_report(inv))
        actual = rep["open_entry_points"]
        expected = [{"asset_id": "e1", "via_asset": "a1",
                     "source_chain": ["a1", "mid", "e1"]}]
        self.assertEqual(actual, expected,
                         f"{case_id}: 可派生新版本的入口应带来源链 expected={expected} actual={actual}")
        self.assertIsNone(rep["earliest_safe_retirement"],
                          f"{case_id}: 入口仍开放时不给退役时点 "
                          f"actual={rep['earliest_safe_retirement']}")
        self.assertEqual(rep["blocking_reasons"], ["open_entry_points"],
                         f"{case_id}: actual={rep['blocking_reasons']}")

    def test_ret09_inactive_entry_point_not_open(self):
        case_id = "RET-09"
        inv = base_inventory()
        inv["assets"] = [
            {"asset_id": "e1", "kind": "entry_point", "active": False},
            {"asset_id": "a1", "kind": "derived", "derives_from": "e1"},
        ]
        add_artifact(inv, asset_id="a1")
        rep = key_report(build_report(inv))
        actual = rep["open_entry_points"]
        self.assertEqual(actual, [],
                         f"{case_id}: 已停用入口不算开放入口 expected=[] actual={actual}")
        self.assertEqual(rep["earliest_safe_retirement"], "2026-01-11T00:00:00Z",
                         f"{case_id}: actual={rep['earliest_safe_retirement']}")

    def test_ret14_derivation_cycle_does_not_hang(self):
        case_id = "RET-14"
        inv = base_inventory()
        inv["assets"] = [
            {"asset_id": "a1", "kind": "derived", "derives_from": "a2"},
            {"asset_id": "a2", "kind": "derived", "derives_from": "a1"},
        ]
        add_artifact(inv, asset_id="a1")
        rep = key_report(build_report(inv))
        actual = rep["open_entry_points"]
        self.assertEqual(actual, [],
                         f"{case_id}: 派生成环且无入口时应安全终止 expected=[] actual={actual}")
        self.assertEqual(rep["earliest_safe_retirement"], "2026-01-11T00:00:00Z",
                         f"{case_id}: actual={rep['earliest_safe_retirement']}")


class EarliestRetirementTests(unittest.TestCase):
    def test_ret10_earliest_is_max_of_retained_expiries(self):
        case_id = "RET-10"
        inv = base_inventory()
        add_artifact(inv, artifact_id="p1", issued_at="2026-01-01T00:00:00Z")
        add_artifact(inv, artifact_id="p2", issued_at="2026-01-03T00:00:00Z")
        rep = key_report(build_report(inv))
        actual = rep["earliest_safe_retirement"]
        expected = "2026-01-13T00:00:00Z"
        self.assertEqual(actual, expected,
                         f"{case_id}: 最早退役时点取存量最晚到期 expected={expected} actual={actual}")
        self.assertEqual(rep["status"], "retain_until",
                         f"{case_id}: actual={rep['status']}")

    def test_ret11_no_stock_retirable_now(self):
        case_id = "RET-11"
        inv = base_inventory()
        rep = key_report(build_report(inv))
        self.assertEqual(rep["status"], "retirable_now",
                         f"{case_id}: actual={rep['status']}")
        self.assertEqual(rep["earliest_safe_retirement"], "2026-01-05T00:00:00Z",
                         f"{case_id}: 完全可退役时时点=盘点时间 "
                         f"actual={rep['earliest_safe_retirement']}")


class ValidationTests(unittest.TestCase):
    def test_ret13_missing_retention_policy_rejected(self):
        case_id = "RET-13"
        inv = base_inventory()
        add_artifact(inv, artifact_type="unknown-type")
        with self.assertRaises(InventoryError) as ctx:
            build_report(inv)
        self.assertIn("缺少验证保留期策略", str(ctx.exception),
                      f"{case_id}: 缺少保留期策略应报输入错误 actual={ctx.exception}")


if __name__ == "__main__":
    unittest.main()
