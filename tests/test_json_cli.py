"""JSON 加载器与命令行入口的验收测试。

所有用例均在进程内构造临时夹具并执行真实断言，不访问网络。
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from governance import GovernanceInputError, evaluate_application, load_application_payload
from governance.cli import main as cli_main


def sample_payload():
    """与 test_engine.build_case 同构的 JSON 夹具（C4 回执过期）。"""
    return {
        "default_go_live_limit": 30.0,
        "assets": [
            {
                "asset_id": "A1",
                "low": 50.0,
                "high": 100.0,
                "required": True,
                "go_live_limit": 30.0,
            }
        ],
        "segments": [
            {"segment_id": "S1", "asset_id": "A1", "low": 40.0, "high": 60.0},
            {"segment_id": "S2", "asset_id": "A1", "low": 10.0, "high": 40.0},
        ],
        "controls": [
            {"control_id": "C1", "coverage": {"S1": 20.0}, "common_cause_group": "G1"},
            {"control_id": "C2", "coverage": {"S1": 35.0}, "common_cause_group": "G1"},
            {"control_id": "C3", "coverage": {"S1": 10.0}},
            {"control_id": "C4", "coverage": {"S2": 25.0}},
        ],
        "receipts": [
            {"receipt_id": "R1", "control_id": "C1", "status": "valid"},
            {"receipt_id": "R2", "control_id": "C2", "status": "valid"},
            {"receipt_id": "R3", "control_id": "C3", "status": "valid"},
            {"receipt_id": "R4", "control_id": "C4", "status": "expired"},
        ],
    }


class JsonLoaderTest(unittest.TestCase):
    """JSON 载荷 -> 裁决模型 -> 裁决结果的端到端断言。"""

    def test_payload_round_trip_produces_suspended_decision(self):
        assets, segments, controls, receipts, default_limit = (
            load_application_payload(sample_payload())
        )
        decision = evaluate_application(
            assets, segments, controls, receipts, default_go_live_limit=default_limit
        )

        self.assertEqual(decision.decision, "suspended")
        self.assertAlmostEqual(decision.breaches[0].residual_high, 55.0)

    def test_optional_fields_have_safe_defaults(self):
        payload = {
            "assets": [
                {"asset_id": "A1", "low": 10.0, "high": 10.0}
            ],
            "segments": [
                {"segment_id": "S1", "asset_id": "A1", "low": 10.0, "high": 10.0}
            ],
            "controls": [
                {"control_id": "C1", "coverage": {"S1": 4.0}}
            ],
            "receipts": [
                {"receipt_id": "R1", "control_id": "C1", "status": "valid"}
            ],
        }
        assets, segments, controls, receipts, default_limit = (
            load_application_payload(payload)
        )

        self.assertTrue(assets[0].required)
        self.assertIsNone(assets[0].go_live_limit)
        self.assertIsNone(controls[0].common_cause_group)
        self.assertEqual(receipts[0].detail, "")
        self.assertIsNone(default_limit)

        decision = evaluate_application(
            assets, segments, controls, receipts, default_go_live_limit=10.0
        )
        self.assertEqual(decision.decision, "proceed")

    def test_non_numeric_value_is_rejected(self):
        payload = sample_payload()
        payload["segments"][0]["low"] = "40"
        with self.assertRaises(GovernanceInputError):
            load_application_payload(payload)

    def test_boolean_is_not_accepted_as_number(self):
        payload = sample_payload()
        payload["assets"][0]["low"] = True
        with self.assertRaises(GovernanceInputError):
            load_application_payload(payload)

    def test_empty_coverage_is_rejected(self):
        payload = sample_payload()
        payload["controls"][0]["coverage"] = {}
        with self.assertRaises(GovernanceInputError):
            load_application_payload(payload)

    def test_root_must_be_object(self):
        with self.assertRaises(GovernanceInputError):
            load_application_payload(["not", "an", "object"])

    def test_cross_constraint_failure_surfaces_after_loading(self):
        # 加载器只做结构校验；段区间之和不等于资产区间由引擎拒绝。
        payload = sample_payload()
        payload["segments"][0]["low"] = 0.0
        assets, segments, controls, receipts, default_limit = (
            load_application_payload(payload)
        )
        with self.assertRaises(GovernanceInputError):
            evaluate_application(
                assets,
                segments,
                controls,
                receipts,
                default_go_live_limit=default_limit,
            )


class CliTest(unittest.TestCase):
    """命令行入口执行真实裁决并输出 JSON。"""

    def _write_payload(self, payload) -> str:
        tmp_dir = tempfile.mkdtemp(prefix="governance-cli-")
        path = os.path.join(tmp_dir, "payload.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        return path

    def test_cli_outputs_decision_json_with_exit_zero(self):
        path = self._write_payload(sample_payload())
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = cli_main([path])

        self.assertEqual(exit_code, 0)
        decoded = json.loads(buffer.getvalue())
        self.assertEqual(decoded["decision"], "suspended")
        self.assertEqual(decoded["breaches"][0]["asset_id"], "A1")

    def test_cli_pretty_mode_remains_valid_json(self):
        path = self._write_payload(sample_payload())
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = cli_main([path, "--pretty"])

        self.assertEqual(exit_code, 0)
        decoded = json.loads(buffer.getvalue())
        self.assertIn("asset_results", decoded)

    def test_cli_missing_file_returns_exit_code_two(self):
        missing = os.path.join(tempfile.gettempdir(), "definitely-missing-11537.json")
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            exit_code = cli_main([missing])

        self.assertEqual(exit_code, 2)
        self.assertIn("裁决输入无效", stderr.getvalue())

    def test_cli_malformed_json_returns_exit_code_two(self):
        tmp_dir = tempfile.mkdtemp(prefix="governance-cli-bad-")
        path = os.path.join(tmp_dir, "bad.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not valid json")

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            exit_code = cli_main([path])
        self.assertEqual(exit_code, 2)


if __name__ == "__main__":
    unittest.main()
