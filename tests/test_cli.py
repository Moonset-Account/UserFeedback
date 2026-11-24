"""CLI 入口的退出码与 JSON 输出。"""

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from route_audit.cli import main

EXAMPLE = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "withdraw_scenario.json"
)
CLEAN_SCENARIO = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "clean_scenario.json"
)


class TestCli(unittest.TestCase):
    def test_exit_nonzero_and_payload_on_finding(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main([str(EXAMPLE)])
        self.assertEqual(code, 1)
        payload = json.loads(buf.getvalue())
        self.assertEqual(len(payload["findings"]), 1)
        self.assertEqual(
            payload["findings"][0]["first_unauthorized_step"]["domain"],
            "edge-b",
        )
        self.assertIn("chg-withdraw-ded-1", payload["traces_by_change"])

    def test_exit_zero_when_compliant(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main([str(CLEAN_SCENARIO)])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["findings"], [])
        self.assertEqual(
            payload["baseline"]["p1"]["status"], "delivered"
        )


if __name__ == "__main__":
    unittest.main()
