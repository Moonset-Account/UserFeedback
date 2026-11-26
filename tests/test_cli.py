import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from transfer_approval.cli import main


class CliTests(unittest.TestCase):
    def test_file_cli_outputs_json_report(self):
        case = {
            "max_delay": 1,
            "key_group": "k",
            "sender_interval": [0, 0],
            "receiver_interval": [1, 1],
            "sender": {"trusted_interval": [0, 0], "windows": [{"start": 0, "end": 2, "key_group": "k"}]},
            "receiver": {"trusted_interval": [1, 1], "windows": [{"start": 0, "end": 2, "key_group": "k"}]},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "case.json"
            path.write_text(json.dumps(case), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                code = main([str(path), "--compact"])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "certain")

    def test_invalid_json_returns_2(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                code = main([str(path)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "invalid_input")

    def test_readme_synthetic_example_is_certain(self):
        example = Path(__file__).resolve().parents[1] / "examples" / "synthetic_case.json"
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([str(example), "--compact"])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "certain")


if __name__ == "__main__":
    unittest.main()
