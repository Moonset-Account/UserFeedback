"""模型层测试：入参约束与半开有效区间判定。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from federated_login.models import (
    AudienceRestriction,
    SubjectConfirmation,
    freeze_restrictions,
)

UTC = timezone.utc
T0 = datetime(2026, 11, 20, 8, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)


def make_confirmation() -> SubjectConfirmation:
    return SubjectConfirmation(
        confirmation_id="c1",
        recipient="https://sp.example.com/acs",
        in_response_to="req-100",
        not_before=T0,
        not_on_or_after=T1,
    )


class SubjectConfirmationModelTest(unittest.TestCase):
    def test_naive_datetime_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SubjectConfirmation(
                confirmation_id="c",
                recipient="r",
                in_response_to="i",
                not_before=datetime(2026, 11, 20, 8, 0, 0),  # 无 tzinfo
                not_on_or_after=T1,
            )

    def test_empty_window_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SubjectConfirmation(
                confirmation_id="c",
                recipient="r",
                in_response_to="i",
                not_before=T1,
                not_on_or_after=T1,  # 右边界必须严格晚于左边界
            )

    def test_half_open_window_left_boundary_inclusive(self) -> None:
        ok, reasons = make_confirmation().evaluate(
            "https://sp.example.com/acs", "req-100", T0
        )
        self.assertTrue(ok, reasons)
        self.assertEqual(reasons, ())

    def test_half_open_window_right_boundary_exclusive(self) -> None:
        ok, reasons = make_confirmation().evaluate(
            "https://sp.example.com/acs", "req-100", T1
        )
        self.assertFalse(ok)
        self.assertEqual(len(reasons), 1)
        self.assertIn("expired", reasons[0])

    def test_inside_window_matches(self) -> None:
        ok, _ = make_confirmation().evaluate(
            "https://sp.example.com/acs", "req-100", T0 + timedelta(minutes=30)
        )
        self.assertTrue(ok)

    def test_before_not_before_is_rejected(self) -> None:
        ok, reasons = make_confirmation().evaluate(
            "https://sp.example.com/acs", "req-100", T0 - timedelta(seconds=1)
        )
        self.assertFalse(ok)
        self.assertIn("not yet valid", reasons[0])

    def test_all_mismatches_are_reported_independently(self) -> None:
        ok, reasons = make_confirmation().evaluate(
            "https://wrong.example.com/acs", "wrong-req", T0
        )
        self.assertFalse(ok)
        self.assertEqual(len(reasons), 2)
        self.assertTrue(any("recipient mismatch" in r for r in reasons))
        self.assertTrue(any("in_response_to mismatch" in r for r in reasons))


class AudienceRestrictionModelTest(unittest.TestCase):
    def test_empty_group_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AudienceRestriction(())

    def test_permits_group_membership(self) -> None:
        group = AudienceRestriction(("app-1", "app-2"))
        self.assertTrue(group.permits("app-1"))
        self.assertTrue(group.permits("app-2"))
        self.assertFalse(group.permits("app-3"))

    def test_freeze_restrictions_normalizes_lists(self) -> None:
        groups = freeze_restrictions([["app-1"], ["app-2", "app-3"]])
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0].audiences, ("app-1",))
        self.assertTrue(groups[1].permits("app-3"))


if __name__ == "__main__":
    unittest.main()
