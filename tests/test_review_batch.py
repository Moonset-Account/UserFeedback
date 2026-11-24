import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from access_review import APPROVE_DUTY, ENTER_DUTY, TimeWindow, review_batch  # noqa: E402


ROLES = {
    "entry_clerk": [ENTER_DUTY],
    "approver": [APPROVE_DUTY],
    "both_duties": [ENTER_DUTY, APPROVE_DUTY],
}


def base_payload(**overrides):
    payload = {
        "batch_id": "B-1",
        "roles": ROLES,
        "existing_grants": [],
        "grants": [],
        "revocations": [],
        "team_grants": [],
        "team_memberships": [],
    }
    payload.update(overrides)
    return payload


class ReviewBatchConflictTests(unittest.TestCase):
    def test_direct_overlapping_grants_return_period_and_sources(self):
        result = review_batch(
            base_payload(
                existing_grants=[
                    {
                        "id": "g-entry",
                        "user_id": "u1",
                        "role_id": "entry_clerk",
                        "project_id": "p1",
                        "valid_from": "2026-01-01T00:00:00Z",
                        "valid_to": "2026-03-01T00:00:00Z",
                    }
                ],
                grants=[
                    {
                        "id": "g-approve",
                        "user_id": "u1",
                        "role_id": "approver",
                        "project_id": "p1",
                        "valid_from": "2026-02-01T00:00:00Z",
                        "valid_to": "2026-04-01T00:00:00Z",
                    }
                ],
            )
        )

        self.assertEqual("suspended", result["status"])
        self.assertEqual("suspend", result["decision"])
        self.assertTrue(result.suspended)
        self.assertFalse(result.allowed)
        self.assertEqual(1, result["conflict_count"])

        conflict = result.conflicts[0]
        self.assertEqual("u1", conflict["user_id"])
        self.assertEqual("p1", conflict["project_id"])
        self.assertEqual(ENTER_DUTY, conflict["entry_duty"])
        self.assertEqual(APPROVE_DUTY, conflict["approval_duty"])

        self.assertEqual(1, len(conflict["entry_sources"]))
        self.assertEqual(1, len(conflict["approval_sources"]))
        self.assertEqual("g-entry", conflict["entry_sources"][0]["grant_id"])
        self.assertEqual("direct", conflict["entry_sources"][0]["source_type"])
        self.assertEqual("g-approve", conflict["approval_sources"][0]["grant_id"])
        self.assertTrue(conflict["approval_sources"][0]["in_batch"])

        overlap = conflict["overlaps"][0]
        self.assertEqual("2026-02-01T00:00:00Z", overlap["start"])
        self.assertEqual("2026-03-01T00:00:00Z", overlap["end"])
        self.assertFalse(overlap["end_inclusive"])
        self.assertEqual("g-entry", overlap["entry_grant_id"])
        self.assertEqual("g-approve", overlap["approval_grant_id"])
        self.assertEqual(conflict["overlap_periods"][0]["start"], overlap["start"])
        self.assertEqual(conflict["overlap_periods"][0]["end"], overlap["end"])

        # The complete report must be JSON-ish plain data.
        import json

        json.dumps(result)

    def test_same_batch_revocation_of_replaced_duty_is_counted(self):
        result = review_batch(
            base_payload(
                existing_grants=[
                    {
                        "id": "old-entry",
                        "user_id": "u1",
                        "role_id": "entry_clerk",
                        "project_id": "p1",
                        "valid_from": "2026-01-01T00:00:00Z",
                        "valid_to": "2026-06-01T00:00:00Z",
                    }
                ],
                revocations=[{"op": "revoke", "grant_id": "old-entry"}],
                grants=[
                    {
                        "id": "new-approval",
                        "user_id": "u1",
                        "role_id": "approver",
                        "project_id": "p1",
                        "valid_from": "2026-02-01T00:00:00Z",
                        "valid_to": "2026-05-01T00:00:00Z",
                    }
                ],
            )
        )

        self.assertEqual("approved", result["status"], result["conflicts"])
        self.assertFalse(result.suspended)
        self.assertIn("old-entry", result["changes"]["revoked_existing_grant_ids"])
        grant_ids = {g["grant_id"] for g in result["effective_sources"]}
        self.assertIn("new-approval", grant_ids)
        self.assertNotIn("old-entry", grant_ids)

    def test_personal_role_revoked_but_team_entry_remains_still_suspends(self):
        result = review_batch(
            base_payload(
                existing_grants=[
                    {
                        "id": "personal-entry",
                        "user_id": "u1",
                        "role_id": "entry_clerk",
                        "project_id": "p1",
                        "valid_from": "2026-01-01",
                        "valid_to": "2026-12-31",
                    }
                ],
                revocations=[
                    {
                        "user_id": "u1",
                        "role_id": "entry_clerk",
                        "project_id": "p1",
                        "source_type": "direct",
                    }
                ],
                grants=[
                    {
                        "id": "enable-approval",
                        "user_id": "u1",
                        "role_id": "approver",
                        "project_id": "p1",
                        "valid_from": "2026-02-15",
                        "valid_to": "2026-03-15",
                    }
                ],
                team_grants=[
                    {
                        "id": "team-entry",
                        "team_id": "finance",
                        "role_id": "entry_clerk",
                        "project_id": "p1",
                        "valid_from": "2026-01-01",
                        "valid_to": "2026-12-31",
                    }
                ],
                team_memberships=[
                    {"user_id": "u1", "team_id": "finance", "id": "m-u1-finance"}
                ],
            )
        )

        self.assertEqual("suspended", result["status"])
        conflict = result.conflicts[0]
        self.assertEqual("p1", conflict["project_id"])

        entry_sources = conflict["entry_sources"]
        self.assertEqual(1, len(entry_sources))
        self.assertEqual("team", entry_sources[0]["source_type"])
        self.assertEqual("team-entry", entry_sources[0]["grant_id"])
        self.assertEqual("finance", entry_sources[0]["team_id"])
        self.assertEqual("m-u1-finance", entry_sources[0]["membership_id"])

        approval_sources = conflict["approval_sources"]
        self.assertEqual(1, len(approval_sources))
        self.assertEqual("direct", approval_sources[0]["source_type"])
        self.assertEqual("enable-approval", approval_sources[0]["grant_id"])
        self.assertTrue(conflict["batch_relevant"])
        self.assertIn("enable-approval", conflict["batch_item_ids"])

        # Date-only validity endpoints are calendar dates and inclusive.
        period = conflict["overlap_periods"][0]
        self.assertEqual("2026-02-15", period["start"])
        self.assertEqual("2026-03-15", period["end"])
        self.assertTrue(period["end_inclusive"])

    def test_different_projects_do_not_conflict(self):
        result = review_batch(
            base_payload(
                grants=[
                    {"id": "e", "user_id": "u1", "role_id": "entry_clerk", "project_id": "p1"},
                    {"id": "a", "user_id": "u1", "role_id": "approver", "project_id": "p2"},
                ]
            )
        )
        self.assertEqual("approved", result["status"])
        self.assertEqual(0, result["conflict_count"])

    def test_non_overlapping_validity_does_not_conflict(self):
        result = review_batch(
            base_payload(
                grants=[
                    {
                        "id": "e",
                        "user_id": "u1",
                        "role_id": "entry_clerk",
                        "project_id": "p1",
                        "valid_from": "2026-01-01T00:00:00Z",
                        "valid_to": "2026-02-01T00:00:00Z",
                    },
                    {
                        "id": "a",
                        "user_id": "u1",
                        "role_id": "approver",
                        "project_id": "p1",
                        "valid_from": "2026-02-01T00:00:00Z",
                        "valid_to": "2026-03-01T00:00:00Z",
                    },
                ]
            )
        )
        self.assertEqual("approved", result["status"])
        self.assertEqual([], result.conflicts)

    def test_role_containing_both_duties_conflicts_with_itself(self):
        result = review_batch(
            base_payload(
                grants=[
                    {"id": "g-both", "user_id": "u9", "role_id": "both_duties", "project_id": "p9"}
                ]
            )
        )
        self.assertEqual("suspended", result["status"])
        conflict = result.conflicts[0]
        grant_ids = {s["grant_id"] for s in conflict["entry_sources"]}
        self.assertEqual({"g-both"}, grant_ids)
        self.assertEqual(
            {"g-both"}, {s["grant_id"] for s in conflict["approval_sources"]}
        )
        self.assertEqual(1, len(conflict["overlaps"]))

    def test_global_project_scope_resolves_against_concrete_project(self):
        result = review_batch(
            base_payload(
                grants=[
                    {"id": "e", "user_id": "u1", "role_id": "entry_clerk", "project_id": "*"},
                    {"id": "a", "user_id": "u1", "role_id": "approver", "project_id": "p42"},
                ]
            )
        )
        self.assertEqual("suspended", result["status"])
        self.assertEqual("p42", result.conflicts[0]["project_id"])

        both_global = review_batch(
            base_payload(
                grants=[
                    {"id": "e", "user_id": "u2", "role_id": "entry_clerk", "project_id": "*"},
                    {"id": "a", "user_id": "u2", "role_id": "approver"},
                ]
            )
        )
        self.assertEqual("*", both_global.conflicts[0]["project_id"])

    def test_interleaved_multiple_validity_segments_return_multiple_overlaps(self):
        result = review_batch(
            base_payload(
                grants=[
                    {
                        "id": "e1",
                        "user_id": "u1",
                        "role_id": "entry_clerk",
                        "project_id": "p1",
                        "valid_from": "2026-01-01T00:00:00Z",
                        "valid_to": "2026-02-01T00:00:00Z",
                    },
                    {
                        "id": "e2",
                        "user_id": "u1",
                        "role_id": "entry_clerk",
                        "project_id": "p1",
                        "valid_from": "2026-03-01T00:00:00Z",
                        "valid_to": "2026-04-01T00:00:00Z",
                    },
                    {
                        "id": "a1",
                        "user_id": "u1",
                        "role_id": "approver",
                        "project_id": "p1",
                        "valid_from": "2026-01-15T00:00:00Z",
                        "valid_to": "2026-03-15T00:00:00Z",
                    },
                ]
            )
        )
        self.assertEqual("suspended", result["status"])
        overlaps = result.conflicts[0]["overlaps"]
        self.assertEqual(2, len(overlaps))
        self.assertEqual("2026-01-15T00:00:00Z", overlaps[0]["start"])
        self.assertEqual("2026-02-01T00:00:00Z", overlaps[0]["end"])
        self.assertEqual("2026-03-01T00:00:00Z", overlaps[1]["start"])
        self.assertEqual("2026-03-15T00:00:00Z", overlaps[1]["end"])
        self.assertEqual({"e1", "e2"}, {o["entry_grant_id"] for o in overlaps})

    def test_changes_list_grant_and_revoke_operations(self):
        result = review_batch(
            {
                "roles": ROLES,
                "changes": [
                    {"op": "revoke", "grant_id": "old-entry"},
                    {
                        "op": "grant",
                        "id": "new-approval",
                        "user_id": "u1",
                        "role_id": "approver",
                        "project_id": "p1",
                    },
                ],
                "existing_grants": [
                    {"id": "old-entry", "user_id": "u1", "role_id": "entry_clerk", "project_id": "p1"}
                ],
            }
        )
        self.assertEqual("approved", result["status"])
        self.assertIn("old-entry", result["changes"]["revoked_existing_grant_ids"])

    def test_explicit_batch_grant_revoked_in_same_batch_is_absent(self):
        result = review_batch(
            base_payload(
                grants=[
                    {"id": "g-approve", "user_id": "u1", "role_id": "approver", "project_id": "p1"},
                    {"id": "g-entry", "user_id": "u1", "role_id": "entry_clerk", "project_id": "p1"},
                ],
                revocations=[{"grant_id": "g-entry"}],
            )
        )
        self.assertEqual("approved", result["status"])
        self.assertIn("g-entry", result["changes"]["revoked_batch_grant_ids"])
        self.assertNotIn(
            "g-entry", {s["grant_id"] for s in result["effective_sources"]}
        )

    def test_chinese_duty_names_are_recognized(self):
        result = review_batch(
            {
                "roles": {
                    "录入员": ["录入收款账户"],
                    "审批员": ["批准收款账户"],
                },
                "grants": [
                    {"id": "e", "user_id": "u1", "role_id": "录入员", "project_id": "p1"},
                    {"id": "a", "user_id": "u1", "role_id": "审批员", "project_id": "p1"},
                ],
            }
        )
        self.assertEqual("suspended", result["status"])
        conflict = result.conflicts[0]
        self.assertEqual(ENTER_DUTY, conflict["entry_duty"])
        self.assertEqual(APPROVE_DUTY, conflict["approval_duty"])

    def test_unknown_role_fails_closed(self):
        result = review_batch(
            base_payload(
                grants=[{"id": "g1", "user_id": "u1", "role_id": "missing", "project_id": "p1"}]
            )
        )
        self.assertEqual("error", result["status"])
        self.assertTrue(result.suspended)
        self.assertFalse(result.allowed)
        self.assertEqual("UNKNOWN_ROLE", result["errors"][0]["code"])


class TimeWindowTests(unittest.TestCase):
    def test_half_open_adjacent_windows_do_not_overlap(self):
        first = TimeWindow("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
        second = TimeWindow("2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z")
        self.assertIsNone(first.intersection(second))

    def test_closed_shared_endpoint_overlaps_at_one_point(self):
        first = TimeWindow(0, 10, True, True)
        second = TimeWindow(10, 20, True, True)
        overlap = first.intersection(second)
        self.assertIsNotNone(overlap)
        self.assertEqual(10, overlap.start)
        self.assertEqual(10, overlap.end)

    def test_unbounded_window_overlaps(self):
        window = TimeWindow.from_value({"valid_from": "2026-01-01T00:00:00Z"})
        overlap = window.intersection(TimeWindow(None, "2026-02-01T00:00:00Z"))
        self.assertEqual("2026-01-01T00:00:00+00:00", overlap.start.isoformat())
        self.assertEqual("2026-02-01T00:00:00+00:00", overlap.end.isoformat())


if __name__ == "__main__":
    unittest.main()
