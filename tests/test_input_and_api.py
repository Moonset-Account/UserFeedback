import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from access_review import SodReviewer, review_batch  # noqa: E402
from review import review_request  # noqa: E402


class InputAndApiTests(unittest.TestCase):
    def test_keyword_arguments_are_accepted(self):
        report = review_batch(
            roles={"approver": ["payment_account:approve"]},
            grants=[
                {"id": "a", "user_id": "u1", "role_id": "approver", "project_id": "p1"}
            ],
        )
        self.assertEqual("approved", report["status"])

    def test_service_alias_and_class_api(self):
        payload = {
            "roles": {"a": ["approve"], "e": ["enter"]},
            "grants": [
                {"id": "a", "user_id": "u1", "role_id": "a", "project_id": "p1"},
                {"id": "e", "user_id": "u1", "role_id": "e", "project_id": "p1"},
            ],
        }
        self.assertEqual("suspended", review_request(payload)["status"])
        self.assertEqual("suspended", SodReviewer().review(payload)["status"])

    def test_empty_revocation_selector_fails_closed(self):
        report = review_batch({"revocations": [{"op": "revoke"}]})
        self.assertEqual("error", report["status"])
        self.assertTrue(report["suspended"])
        self.assertEqual("INVALID_REVOCATION", report["errors"][0]["code"])

    def test_unrecognized_batch_item_fails_closed(self):
        report = review_batch({"items": [{"unexpected": True}]})
        self.assertEqual("error", report["status"])
        self.assertTrue(report["suspended"])
        self.assertEqual("UNRECOGNIZED_BATCH_ITEM", report["errors"][0]["code"])

    def test_grant_can_reference_multiple_roles(self):
        report = review_batch(
            {
                "roles": {
                    "entry": ["enter"],
                    "approval": ["approve"],
                },
                "grants": [
                    {
                        "id": "g1",
                        "user_id": "u1",
                        "roles": ["entry", "approval"],
                        "project_id": "p1",
                    }
                ],
            }
        )
        self.assertEqual("suspended", report["status"])
        self.assertEqual(1, report["conflict_count"])
        self.assertEqual(["entry", "approval"], report["effective_sources"][0]["role_ids"])


if __name__ == "__main__":
    unittest.main()
