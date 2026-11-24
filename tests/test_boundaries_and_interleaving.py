from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from authorization_checker import (  # noqa: E402
    Approval,
    ApprovalKind,
    AuthorizationChecker,
    Identity,
    Interval,
    Scope,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 1, 10, tzinfo=UTC)
NOON_5 = datetime(2026, 1, 5, 12, tzinfo=UTC)
THIRTEEN_5 = NOON_5 + timedelta(hours=1)


def approval(
    approval_id: str,
    kind: ApprovalKind,
    person_id: str,
    start: datetime,
    end: datetime,
) -> Approval:
    return Approval(
        approval_id=approval_id,
        kind=kind,
        person_id=person_id,
        identity=f"corp:{person_id}",
        interval=Interval(start, end),
        scope=Scope.of(project="p", service="svc"),
    )


class BoundaryAndInterleavingTests(unittest.TestCase):
    def test_touching_windows_handoff_at_exact_boundary(self) -> None:
        checker = AuthorizationChecker(
            [
                approval("bo", ApprovalKind.BUSINESS_OWNER, "alice", T0, T1),
                approval("ac-b", ApprovalKind.ACCOUNT_CUSTODIAN, "bob", T0, NOON_5),
                approval("ac-c", ApprovalKind.ACCOUNT_CUSTODIAN, "carol", NOON_5, T1),
            ]
        )
        decision = checker.evaluate(
            target_service="svc",
            project_id="p",
            interval=Interval(T0, T1),
        )

        self.assertTrue(decision.fully_authorized)
        self.assertEqual(len(decision.grants), 2)
        self.assertEqual(decision.grants[0].end, NOON_5)
        self.assertEqual(decision.grants[1].start, NOON_5)
        self.assertEqual(decision.gaps, ())

    def test_one_instant_gap_is_reported(self) -> None:
        checker = AuthorizationChecker(
            [
                approval("bo", ApprovalKind.BUSINESS_OWNER, "alice", T0, T1),
                approval("ac-b", ApprovalKind.ACCOUNT_CUSTODIAN, "bob", T0, NOON_5),
                approval("ac-c", ApprovalKind.ACCOUNT_CUSTODIAN, "carol", THIRTEEN_5, T1),
            ]
        )
        decision = checker.evaluate(
            target_service="svc",
            project_id="p",
            interval=Interval(T0, T1),
        )

        self.assertFalse(decision.fully_authorized)
        self.assertEqual(len(decision.grants), 2)
        self.assertEqual(len(decision.gaps), 1)
        gap = decision.gaps[0]
        self.assertEqual((gap.start, gap.end), (NOON_5, THIRTEEN_5))

    def test_overlapping_alternative_pairs_are_all_listed(self) -> None:
        checker = AuthorizationChecker(
            [
                approval("bo-a", ApprovalKind.BUSINESS_OWNER, "alice", T0, T1),
                approval("bo-d", ApprovalKind.BUSINESS_OWNER, "dave", NOON_5, T1),
                approval("ac-b", ApprovalKind.ACCOUNT_CUSTODIAN, "bob", T0, T1),
                approval("ac-c", ApprovalKind.ACCOUNT_CUSTODIAN, "carol", NOON_5, T1),
            ]
        )
        decision = checker.evaluate(
            target_service="svc",
            project_id="p",
            interval=Interval(T0, T1),
        )

        pair_keys = {
            (
                grant.responsible.business_owner.person_id,
                grant.responsible.account_custodian.person_id,
            )
            for grant in decision.feasible_pair_intervals
        }
        self.assertIn(("alice", "bob"), pair_keys)
        self.assertIn(("alice", "carol"), pair_keys)
        self.assertIn(("dave", "bob"), pair_keys)
        self.assertIn(("dave", "carol"), pair_keys)

        # The deterministic primary timeline remains non-overlapping.
        for left, right in zip(decision.grants, decision.grants[1:]):
            self.assertLessEqual(left.end, right.start)

    def test_adjacent_same_person_approvals_merge_to_one_grant(self) -> None:
        # Two approvals from the same person touch and should represent one
        # continuous responsible-person interval.
        checker = AuthorizationChecker(
            [
                approval("bo-1", ApprovalKind.BUSINESS_OWNER, "alice", T0, NOON_5),
                approval("bo-2", ApprovalKind.BUSINESS_OWNER, "alice", NOON_5, T1),
                approval("ac", ApprovalKind.ACCOUNT_CUSTODIAN, "bob", T0, T1),
            ]
        )
        decision = checker.evaluate(
            target_service="svc",
            project_id="p",
            interval=Interval(T0, T1),
        )

        self.assertEqual(len(decision.grants), 1)
        grant = decision.grants[0]
        self.assertEqual((grant.start, grant.end), (T0, T1))
        self.assertEqual(
            grant.responsible.business_owner.approval_ids,
            ("bo-1", "bo-2"),
        )

    def test_cross_directory_identity_handoff_stays_one_responsible_person(self) -> None:
        checker = AuthorizationChecker(
            [
                Approval(
                    approval_id="bo-corp",
                    kind=ApprovalKind.BUSINESS_OWNER,
                    identity=Identity("corp", "alice"),
                    interval=Interval(T0, NOON_5),
                    scope=Scope.of(project="p", service="svc"),
                ),
                Approval(
                    approval_id="bo-partner",
                    kind=ApprovalKind.BUSINESS_OWNER,
                    identity=Identity("partner-portal", "alice@partner.test"),
                    interval=Interval(NOON_5, T1),
                    scope=Scope.of(project="p", service="svc"),
                ),
                approval("ac", ApprovalKind.ACCOUNT_CUSTODIAN, "bob", T0, T1),
            ],
            identity_links=[
                ("alice", "corp", "alice"),
                ("alice", "partner-portal", "alice@partner.test"),
            ],
        )
        decision = checker.evaluate(
            target_service="svc",
            project_id="p",
            interval=Interval(T0, T1),
        )

        self.assertTrue(decision.fully_authorized)
        self.assertEqual(len(decision.grants), 1)
        business = decision.grants[0].responsible.business_owner
        self.assertEqual(business.person_id, "alice")
        self.assertEqual(business.approval_ids, ("bo-corp", "bo-partner"))
        self.assertEqual(
            business.identities,
            ("corp:alice", "partner-portal:alice@partner.test"),
        )

    def test_invalid_interval_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Interval(T1, T0)


if __name__ == "__main__":
    unittest.main()
