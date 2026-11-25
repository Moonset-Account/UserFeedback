import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from synthetic_data import build_scenario

from volume_audit.cli import main


class CliTests(unittest.TestCase):
    def test_json_and_text_modes_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = Path(tmp) / "scenario.json"
            scenario.write_text(json.dumps(build_scenario()), encoding="utf-8")

            output_path = Path(tmp) / "report.json"
            code = main([str(scenario), "--output", str(output_path)])
            self.assertEqual(code, 0)
            report = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["protected_count"], 1)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main([str(scenario), "--format", "text"])
            self.assertEqual(code, 0)
            text = buffer.getvalue()
            self.assertIn("Volume handoff audit", text)
            self.assertIn("historical protected", text)
            self.assertIn("R-OLD-DATA-1", text)

    def test_missing_file_returns_error_code(self):
        code = main(["/definitely/not/here/volume-audit-scenario.json"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
