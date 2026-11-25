from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from blind_telemetry.demo import main


class DemoScriptTests(unittest.TestCase):
    def test_demo_runs_all_required_scenarios(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = main()

        text = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("normal report accepted", text)
        self.assertIn("original BlindReply", text)
        self.assertIn("different blind for same device/period", text)
        self.assertIn("two injected factors", text)
        self.assertIn("same-period duplicate report", text)
        self.assertIn("cross-period replay", text)
        self.assertIn("concurrent double submission outcomes", text)
        self.assertIn("demo completed successfully", text)


if __name__ == "__main__":
    unittest.main()
