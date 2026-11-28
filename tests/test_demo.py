"""The demo script must run end-to-end and honour the scenario contract."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DemoTests(unittest.TestCase):
    def test_demo_runs_successfully(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "demo.py")],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout
        for expected in ("Independent identities", "Wrong merge",
                         "Both partners register at the same time",
                         "Split back apart", "Replay",
                         "share no identifier", "common handles: []",
                         "common identifiers overall: []"):
            self.assertIn(expected, output)
        self.assertNotIn("Traceback", result.stderr)

    def test_demo_module_main_runs(self):
        sys.path.insert(0, str(ROOT))
        try:
            import demo
        finally:
            sys.path.pop(0)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            demo.main()
        self.assertIn("common identifiers overall: []", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
