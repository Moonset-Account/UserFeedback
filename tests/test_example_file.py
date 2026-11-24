import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from disclosure_review.__main__ import main
from disclosure_review.api import review_dict


class SyntheticExampleFileTests(unittest.TestCase):
    def setUp(self):
        self.path = ROOT / "examples" / "synthetic_case.json"
        self.payload = json.loads(self.path.read_text(encoding="utf-8"))

    def test_example_has_expected_normal_nested_interleaved_and_missing_ranges(self):
        report = review_dict(self.payload)
        self.assertEqual(report["disclosable_ranges"], [[1, 3], [5, 7], [9, 10]])
        self.assertEqual([item["range"] for item in report["withheld_ranges"]], [[4, 4], [8, 8], [11, 11]])
        self.assertIn("NOT_IN_APPROVED_SCOPE", report["withheld_ranges"][0]["reason_codes"])
        self.assertIn("NOT_IN_APPROVED_SCOPE", report["withheld_ranges"][1]["reason_codes"])
        self.assertEqual(report["remediations"][0]["repair_from"]["record_id"], "payroll-register")

    def test_cli_outputs_json(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = main([str(self.path), "--no-traces", "--compact"])
        self.assertEqual(exit_code, 0)
        encoded = buffer.getvalue()
        decoded = json.loads(encoded)
        self.assertEqual(decoded["summary"]["total_paragraphs"], 11)


if __name__ == "__main__":
    unittest.main()
