import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from disclosure_review import review_dict


class DictionaryApiTests(unittest.TestCase):
    def test_json_synthetic_case_end_to_end(self):
        payload = {
            "submission": {
                "record_id": "statement-json",
                "version": "1",
                "paragraphs": [
                    "Intro",
                    "Approved outer and inner",
                    "Outer approved but inner missing",
                ],
                "citations": [
                    {
                        "citation_id": "s-to-i",
                        "holder_range": [2, 3],
                        "source": "interview-json@1",
                        "source_range": [1, 2],
                    }
                ],
            },
            "records": [
                {
                    "record_id": "interview-json",
                    "version": "1",
                    "kind": "interview",
                    "paragraphs": ["I1", "I2 cites missing memo"],
                    "citations": [
                        {
                            "citation_id": "i-to-missing",
                            "holder_range": 2,
                            "source": "memo-missing@1",
                            "source_range": 1,
                        }
                    ],
                }
            ],
            "approvals": [
                {
                    "record_id": "interview-json",
                    "version": "1",
                    "approved_ranges": [[1, 2]],
                }
            ],
        }
        result = review_dict(payload)
        # Must be JSON serializable.
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertIn("MATERIAL_NOT_FOUND", encoded)

        self.assertEqual(result["summary"]["disclosable_paragraphs"], [1, 2])
        self.assertEqual(result["summary"]["withheld_paragraphs"], [3])
        self.assertEqual(result["summary"]["disclosable_ranges"], [[1, 2]])
        self.assertEqual(result["withheld_ranges"][0]["range"], [3, 3])
        repair = result["remediations"][0]["repair_from"]
        self.assertEqual(repair["record_id"], "memo-missing")

    def test_revision_payload_auto_binds_approval_hashes(self):
        payload = {
            "submission": {
                "record_id": "stmt-json-rev",
                "version": "2",
                "paragraphs": ["unchanged", "changed wording"],
            },
            "records": [
                {
                    "record_id": "stmt-json-rev",
                    "version": "1",
                    "paragraphs": ["unchanged", "old wording"],
                }
            ],
            "approvals": [
                {
                    "record_id": "stmt-json-rev",
                    "version": "1",
                    "approved_ranges": [[1, 2]],
                    "approval_id": "old-review",
                }
            ],
            "carry_forwards": [
                {
                    "target": "stmt-json-rev@2",
                    "source": "stmt-json-rev@1",
                    "paragraph_map": {"1": 1},
                }
            ],
            "baseline_submission": "stmt-json-rev@1",
        }
        result = review_dict(payload)
        self.assertEqual(result["paragraphs"][0]["status"], "disclosable")
        self.assertEqual(result["paragraphs"][1]["status"], "withheld")
        self.assertIn(
            "REWRITTEN_NEEDS_CONFIRMATION",
            result["paragraphs"][1]["reason_codes"],
        )


if __name__ == "__main__":
    unittest.main()
