"""基于 SCENARIOS 夹具的判定结论测试。"""
from __future__ import annotations

import unittest

from vuln_ledger.engine import evaluate
from vuln_ledger.models import InputError
from vuln_ledger.parser import parse_input

from tests.scenarios import ERROR_SCENARIOS, SCENARIOS


class ScenarioStatusTest(unittest.TestCase):
    def _deployment_by_id(self, result: dict, build_id: str) -> dict:
        for dep in result["deployments"]:
            if dep["build_id"] == build_id:
                return dep
        self.fail(f"结果中缺少部署 {build_id}")

    def test_all_scenarios(self) -> None:
        for scenario in SCENARIOS:
            with self.subTest(case_id=scenario["case_id"]):
                ledger = parse_input(scenario["input"])
                result = evaluate(ledger)
                self.assertEqual(
                    len(result["deployments"]),
                    len(scenario["expected"]),
                    f"{scenario['case_id']} 部署数量不匹配",
                )
                for build_id, expected in scenario["expected"].items():
                    dep = self._deployment_by_id(result, build_id)
                    self.assertEqual(
                        dep["status"],
                        expected["status"],
                        f"{scenario['case_id']}/{build_id} 总体状态",
                    )
                    actual_codes = sorted(m["code"] for m in dep["missing_records"])
                    self.assertEqual(
                        actual_codes,
                        sorted(expected["missing_codes"]),
                        f"{scenario['case_id']}/{build_id} 缺失记录 code 列表",
                    )
                    for fid, expected_fix_status in expected["fix"].items():
                        self.assertIn(
                            fid,
                            dep["fix_statuses"],
                            f"{scenario['case_id']}/{build_id} 缺少修复标识 {fid}",
                        )
                        self.assertEqual(
                            dep["fix_statuses"][fid]["status"],
                            expected_fix_status,
                            f"{scenario['case_id']}/{build_id}/{fid} 单修复状态",
                        )

    def test_error_scenarios(self) -> None:
        for scenario in ERROR_SCENARIOS:
            with self.subTest(case_id=scenario["case_id"]):
                with self.assertRaises(InputError) as ctx:
                    parse_input(scenario["input"])
                self.assertIn(scenario["error_contains"], str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
