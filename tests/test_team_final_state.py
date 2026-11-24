import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from access_review import ENTER_DUTY, APPROVE_DUTY  # noqa: E402
from review import review_batch as top_level_review  # noqa: E402


ROLES = {
    "entry": [ENTER_DUTY],
    "approve": [APPROVE_DUTY],
}


class TeamValidityAndFinalStateTests(unittest.TestCase):
    def test_team_membership_validity_can_remove_overlap(self):
        result = top_level_review(
            {
                "roles": ROLES,
                "team_grants": [
                    {
                        "id": "tg-entry",
                        "team_id": "finance",
                        "role_id": "entry",
                        "project_id": "p1",
                        "valid_from": "2026-01-01T00:00:00Z",
                        "valid_to": "2026-07-01T00:00:00Z",
                    }
                ],
                "team_memberships": [
                    {
                        "id": "m1",
                        "user_id": "u1",
                        "team_id": "finance",
                        "valid_from": "2026-01-01T00:00:00Z",
                        "valid_to": "2026-02-01T00:00:00Z",
                    }
                ],
                "grants": [
                    {
                        "id": "g-approve",
                        "user_id": "u1",
                        "role_id": "approve",
                        "project_id": "p1",
                        "valid_from": "2026-03-01T00:00:00Z",
                        "valid_to": "2026-04-01T00:00:00Z",
                    }
                ],
            }
        )
        self.assertEqual("approved", result["status"], result.conflicts)
        self.assertEqual(0, result["conflict_count"])

    def test_team_membership_interval_narrows_source_validity(self):
        result = top_level_review(
            {
                "roles": ROLES,
                "team_grants": [
                    {
                        "id": "tg-entry",
                        "team_id": "finance",
                        "role_id": "entry",
                        "project_id": "p1",
                        "valid_from": "2026-01-01T00:00:00Z",
                        "valid_to": "2026-07-01T00:00:00Z",
                    }
                ],
                "team_memberships": [
                    {
                        "id": "m1",
                        "user_id": "u1",
                        "team_id": "finance",
                        "valid_from": "2026-01-15T00:00:00Z",
                        "valid_to": "2026-03-01T00:00:00Z",
                    }
                ],
                "grants": [
                    {
                        "id": "g-approve",
                        "user_id": "u1",
                        "role_id": "approve",
                        "project_id": "p1",
                        "valid_from": "2026-02-01T00:00:00Z",
                        "valid_to": "2026-05-01T00:00:00Z",
                    }
                ],
            }
        )
        self.assertEqual("suspended", result["status"])
        source = result.conflicts[0]["entry_sources"][0]
        self.assertEqual("2026-01-15T00:00:00Z", source["validity"]["start"])
        self.assertEqual("2026-03-01T00:00:00Z", source["validity"]["end"])
        overlap = result.conflicts[0]["overlap_periods"][0]
        self.assertEqual("2026-02-01T00:00:00Z", overlap["start"])
        self.assertEqual("2026-03-01T00:00:00Z", overlap["end"])

    def test_same_batch_team_grant_revocation_clears_conflict(self):
        result = top_level_review(
            {
                "roles": ROLES,
                "team_grants": [
                    {
                        "id": "tg-entry",
                        "team_id": "finance",
                        "role_id": "entry",
                        "project_id": "p1",
                    }
                ],
                "team_memberships": [{"user_id": "u1", "team_id": "finance"}],
                "revocations": [{"grant_id": "tg-entry"}],
                "grants": [
                    {"id": "g-approve", "user_id": "u1", "role_id": "approve", "project_id": "p1"}
                ],
            }
        )
        self.assertEqual("approved", result["status"], result.conflicts)
        self.assertIn("tg-entry", result["changes"]["revoked_team_grant_ids"])

    def test_natural_personal_revocation_does_not_revoke_team_grant(self):
        result = top_level_review(
            {
                "roles": ROLES,
                "existing_grants": [
                    {"id": "personal-entry", "user_id": "u1", "role_id": "entry", "project_id": "p1"}
                ],
                "revocations": [
                    {"user_id": "u1", "role_id": "entry", "project_id": "p1"}
                ],
                "team_grants": [
                    {"id": "team-entry", "team_id": "finance", "role_id": "entry", "project_id": "p1"}
                ],
                "team_memberships": [{"user_id": "u1", "team_id": "finance"}],
                "grants": [
                    {"id": "approval", "user_id": "u1", "role_id": "approve", "project_id": "p1"}
                ],
            }
        )
        self.assertEqual("suspended", result["status"])
        self.assertIn("personal-entry", result["changes"]["revoked_existing_grant_ids"])
        self.assertNotIn("team-entry", result["changes"]["revoked_team_grant_ids"])
        self.assertEqual(
            ["team-entry"],
            [s["grant_id"] for s in result.conflicts[0]["entry_sources"]],
        )

    def test_nested_batch_payload_is_unwrapped(self):
        result = top_level_review(
            {
                "roles": ROLES,
                "batch": {
                    "batch_id": "nested",
                    "grants": [
                        {"id": "e", "user_id": "u1", "role_id": "entry", "project_id": "p1"},
                        {"id": "a", "user_id": "u1", "role_id": "approve", "project_id": "p1"},
                    ],
                },
            }
        )
        self.assertEqual("nested", result["batch_id"])
        self.assertEqual("suspended", result["status"])

    def test_team_grant_inside_changes_is_routed_to_team_sources(self):
        result = top_level_review(
            {
                "roles": ROLES,
                "team_memberships": [{"user_id": "u1", "team_id": "finance"}],
                "changes": [
                    {
                        "op": "grant",
                        "id": "change-team-entry",
                        "principal_type": "team",
                        "team_id": "finance",
                        "role_id": "entry",
                        "project_id": "p1",
                    },
                    {
                        "op": "grant",
                        "id": "change-approval",
                        "user_id": "u1",
                        "role_id": "approve",
                        "project_id": "p1",
                    },
                ],
            }
        )
        self.assertEqual("suspended", result["status"], result.get("errors"))
        source = result.conflicts[0]["entry_sources"][0]
        self.assertEqual("team", source["source_type"])
        self.assertEqual("change-team-entry", source["grant_id"])
        self.assertTrue(source["in_batch"])


if __name__ == "__main__":
    unittest.main()
