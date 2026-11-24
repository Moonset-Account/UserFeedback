from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from authorization_checker import (  # noqa: E402
    UnresolvedIdentityError,
    evaluate_authorization,
)


def approval(
    approval_id: str,
    kind: str,
    person_id: str,
    valid_from: str = "2026-01-01",
    valid_until: str = "2026-04-01",
    **extra: object,
) -> dict[str, object]:
    data: dict[str, object] = {
        "id": approval_id,
        "type": kind,
        "person_id": person_id,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "project": "proj-1",
        "service": "shared-sa",
    }
    data.update(extra)
    return data


def evaluate(approvals: list[dict[str, object]], **extra: object):
    payload: dict[str, object] = {
        "target_service": "shared-sa",
        "project": "proj-1",
        "requested_interval": {"start": "2026-01-01", "end": "2026-04-01"},
        "approvals": approvals,
    }
    payload.update(extra)
    return evaluate_authorization(payload)


class AuthorizationEngineTests(unittest.TestCase):
    def test_distinct_owners_produce_partial_interval_and_gaps(self) -> None:
        decision = evaluate(
            [
                approval("bo-1", "business_owner", "alice", valid_until="2026-03-01"),
                approval(
                    "ac-1",
                    "account_custodian",
                    "bob",
                    valid_from="2026-01-15",
                    valid_until="2026-02-15",
                ),
            ]
        )

        self.assertEqual(len(decision.grants), 1)
        grant = decision.grants[0]
        self.assertEqual(grant.start, date(2026, 1, 15))
        self.assertEqual(grant.end, date(2026, 2, 15))
        self.assertEqual(
            grant.responsible.business_owner.person_id, "alice"
        )
        self.assertEqual(
            grant.responsible.account_custodian.person_id, "bob"
        )
        self.assertFalse(decision.fully_authorized)
        self.assertTrue(decision.grantable)
        self.assertEqual(
            [(gap.start, gap.end) for gap in decision.gaps],
            [
                (date(2026, 1, 1), date(2026, 1, 15)),
                (date(2026, 2, 15), date(2026, 4, 1)),
            ],
        )

    def test_interleaving_windows_switch_responsible_combo(self) -> None:
        decision = evaluate(
            [
                approval("bo-a", "business_owner", "alice"),
                approval(
                    "ac-b",
                    "account_custodian",
                    "bob",
                    valid_until="2026-02-01",
                ),
                approval(
                    "ac-c",
                    "account_custodian",
                    "carol",
                    valid_from="2026-02-01",
                ),
            ]
        )

        self.assertTrue(decision.fully_authorized)
        self.assertEqual(decision.gaps, ())
        self.assertEqual(len(decision.continuous_coverage), 1)
        self.assertEqual(
            decision.continuous_coverage[0].start, date(2026, 1, 1)
        )
        self.assertEqual(decision.continuous_coverage[0].end, date(2026, 4, 1))

        self.assertEqual(len(decision.grants), 2)
        first, second = decision.grants
        self.assertEqual((first.start, first.end), (date(2026, 1, 1), date(2026, 2, 1)))
        self.assertEqual(first.responsible.business_owner.person_id, "alice")
        self.assertEqual(first.responsible.account_custodian.person_id, "bob")
        self.assertEqual((second.start, second.end), (date(2026, 2, 1), date(2026, 4, 1)))
        self.assertEqual(second.responsible.business_owner.person_id, "alice")
        self.assertEqual(second.responsible.account_custodian.person_id, "carol")

    def test_multiple_directories_resolve_to_one_natural_person(self) -> None:
        decision = evaluate(
            [
                {
                    "id": "bo-alice-corp",
                    "type": "business_owner",
                    "identity": {"directory": "corp", "subject": "alice"},
                    "valid_from": "2026-01-01",
                    "valid_until": "2026-04-01",
                    "project": "proj-1",
                    "service": "shared-sa",
                },
                {
                    "id": "ac-alice-external",
                    "type": "custodian",
                    "identity": "external:alice@example.test",
                    "valid_from": "2026-01-01",
                    "valid_until": "2026-04-01",
                    "project": "proj-1",
                    "service": "shared-sa",
                },
            ],
            identity_links=[
                {
                    "person_id": "alice",
                    "identities": [
                        {"directory": "corp", "subject": "alice"},
                        "external:alice@example.test",
                    ],
                }
            ],
        )

        self.assertEqual(decision.grants, ())
        report = decision.independence
        self.assertEqual(report.status, "same_person_only")
        self.assertEqual(report.same_person_ids, ("alice",))
        self.assertIsNone(report.primary_missing_independent_approval_type)
        self.assertEqual(
            set(report.missing_independent_approval_types),
            {"business_owner", "account_custodian"},
        )
        self.assertEqual(len(report.requirement_groups), 1)
        self.assertEqual(len(report.requirement_groups[0]), 2)

    def test_two_same_type_approvals_from_one_person_name_missing_category(
        self,
    ) -> None:
        decision = evaluate(
            [
                {
                    "id": "bo-1",
                    "type": "business_owner",
                    "identity": "corp:alice",
                    "valid_from": "2026-01-01",
                    "valid_until": "2026-04-01",
                    "project": "proj-1",
                    "service": "shared-sa",
                },
                {
                    "id": "bo-2",
                    "type": "business",
                    "identity": "external:alice@example.test",
                    "valid_from": "2026-01-01",
                    "valid_until": "2026-04-01",
                    "project": "proj-1",
                    "service": "shared-sa",
                },
            ],
            identity_links=[
                ("alice", "corp", "alice"),
                ("alice", "external", "alice@example.test"),
            ],
        )

        self.assertEqual(decision.grants, ())
        self.assertEqual(
            decision.independence.status, "missing_account_custodian"
        )
        self.assertEqual(
            decision.primary_missing_independent_approval_type,
            "account_custodian",
        )
        self.assertEqual(
            decision.missing_independent_approval_types,
            ["account_custodian"],
        )
        option = decision.independence.requirement_groups[0][0]
        self.assertEqual(option.distinct_from_person_id, "alice")

    def test_scope_mismatch_is_not_counted(self) -> None:
        decision = evaluate(
            [
                approval("bo", "business_owner", "alice", project="other-project"),
                approval("ac", "account_custodian", "bob", service="other-service"),
            ]
        )
        self.assertEqual(decision.grants, ())
        self.assertEqual(decision.independence.status, "missing_both_types")

    def test_expired_distinct_approvals_give_no_interval_but_scope_diagnosis(
        self,
    ) -> None:
        decision = evaluate(
            [
                approval(
                    "bo",
                    "business_owner",
                    "alice",
                    valid_from="2025-10-01",
                    valid_until="2025-12-31",
                ),
                approval(
                    "ac",
                    "account_custodian",
                    "bob",
                    valid_from="2025-10-01",
                    valid_until="2025-12-31",
                ),
            ]
        )
        self.assertTrue(decision.independence.has_independent_pair)
        self.assertEqual(decision.grants, ())
        self.assertEqual(len(decision.gaps), 1)
        self.assertTrue(
            any(f.code == "approval_outside_requested_interval" for f in decision.findings)
        )

    def test_inclusive_date_valid_to_is_normalized(self) -> None:
        decision = evaluate(
            [
                {
                    "id": "bo",
                    "type": "bo",
                    "person_id": "alice",
                    "valid_from": "2026-01-01",
                    "valid_to": "2026-02-28",
                    "project": "proj-1",
                    "service": "shared-sa",
                },
                {
                    "id": "ac",
                    "type": "ac",
                    "person_id": "bob",
                    "valid_from": "2026-01-15",
                    "valid_to": "2026-02-14",
                    "project": "proj-1",
                    "service": "shared-sa",
                },
            ]
        )
        self.assertEqual(len(decision.grants), 1)
        self.assertEqual(decision.grants[0].end, date(2026, 2, 15))

    def test_strict_unresolved_identity_raises(self) -> None:
        payload = {
            "target_service": "shared-sa",
            "project": "proj-1",
            "start": "2026-01-01",
            "end": "2026-04-01",
            "strict_identities": True,
            "approvals": [
                {
                    "id": "bo",
                    "type": "business_owner",
                    "approver": "corp:alice-only",
                    "valid_from": "2026-01-01",
                    "valid_until": "2026-04-01",
                }
            ],
        }
        with self.assertRaises(UnresolvedIdentityError):
            evaluate_authorization(payload)

    def test_nonstrict_unresolved_identities_fail_closed(self) -> None:
        payload = {
            "target_service": "shared-sa",
            "project": "proj-1",
            "start": "2026-01-01",
            "end": "2026-04-01",
            "strict_identities": False,
            "approvals": [
                {
                    "id": "bo",
                    "type": "business_owner",
                    "identity": "corp:alice-one",
                    "valid_from": "2026-01-01",
                    "valid_until": "2026-04-01",
                },
                {
                    "id": "ac",
                    "type": "custodian",
                    "identity": "external:alice-two",
                    "valid_from": "2026-01-01",
                    "valid_until": "2026-04-01",
                },
            ],
        }
        decision = evaluate_authorization(payload)
        self.assertEqual(decision.grants, ())
        self.assertFalse(decision.identity_assurance_complete)
        self.assertTrue(
            any(f.code == "unresolved_identity" for f in decision.findings)
        )

    def test_decision_is_json_serializable(self) -> None:
        decision = evaluate(
            [
                approval("bo", "business_owner", "alice"),
                approval("ac", "custodian", "bob"),
            ]
        )
        text = json.dumps(decision.to_dict(), ensure_ascii=False)
        self.assertIn("business_owner", text)
        self.assertIn("account_custodian", text)


if __name__ == "__main__":
    unittest.main()
