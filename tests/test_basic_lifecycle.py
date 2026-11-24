"""Normal lifecycle: ordinary retention, expiry boundary and view routing."""

import unittest
from datetime import date

from retention_decisions import (
    DerivedArtifact,
    Material,
    PreservationNotice,
    RetentionEngine,
    RetentionStatus as S,
)


def build_engine() -> RetentionEngine:
    engine = RetentionEngine()
    engine.register_material(
        Material.create(
            "case-1",
            ["name", "ssn", "address", "notes"],
            date(2027, 1, 1),
        )
    )
    # Hold covers two columns (ssn, name); address and notes keep ageing
    # on the original schedule.
    engine.register_notice(
        PreservationNotice.create(
            "N-1", "case-1", ["ssn", "name"], date(2026, 6, 1)
        )
    )
    engine.register_artifact(
        DerivedArtifact.create("A-summary", "case-1", ["name", "address"])
    )
    engine.register_artifact(
        DerivedArtifact.create("A-ssn", "case-1", ["ssn"])
    )
    engine.register_artifact(
        DerivedArtifact.create(
            "A-full", "case-1", ["name", "ssn", "address", "notes"]
        )
    )
    engine.register_artifact(
        DerivedArtifact.create("A-addr", "case-1", ["address"])
    )
    return engine


class BasicLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = build_engine()

    def test_all_normal_before_notice_and_expiry(self) -> None:
        decision = self.engine.decide("case-1", date(2026, 3, 1))
        self.assertEqual(
            set(decision.normal_view_fields),
            {"name", "ssn", "address", "notes"},
        )
        self.assertEqual(decision.controlled_view_fields, ())
        self.assertEqual(decision.withdrawn_fields, ())
        for field in decision.field_decisions:
            self.assertIs(field.status, S.NORMAL)
            self.assertEqual(field.active_notice_ids, ())
        for artifact in decision.artifact_decisions:
            self.assertIs(artifact.status, S.NORMAL)
        self.assertEqual(
            set(decision.normal_view_artifacts),
            {"A-summary", "A-ssn", "A-full", "A-addr"},
        )

    def test_day_before_notice_effective_date_is_still_normal(self) -> None:
        decision = self.engine.decide("case-1", date(2026, 5, 31))
        self.assertIs(decision.field_status("ssn"), S.NORMAL)
        self.assertIs(decision.field_status("name"), S.NORMAL)

    def test_notice_routes_held_columns_to_controlled_view(self) -> None:
        decision = self.engine.decide("case-1", date(2026, 6, 1))
        self.assertIs(decision.field_status("ssn"), S.PRESERVED)
        self.assertIs(decision.field_status("name"), S.PRESERVED)
        # Columns outside the two-column hold stay on the normal schedule.
        self.assertIs(decision.field_status("address"), S.NORMAL)
        self.assertIs(decision.field_status("notes"), S.NORMAL)
        self.assertEqual(
            set(decision.controlled_view_fields), {"name", "ssn"}
        )
        self.assertEqual(set(decision.withdrawn_fields), {"name", "ssn"})
        self.assertEqual(
            set(decision.normal_view_fields), {"address", "notes"}
        )
        held = {
            d.field_name: d.active_notice_ids
            for d in decision.field_decisions
            if d.status is S.PRESERVED
        }
        self.assertEqual(held, {"name": ("N-1",), "ssn": ("N-1",)})

    def test_day_before_normal_expiry_remaining_columns_still_normal(self) -> None:
        decision = self.engine.decide("case-1", date(2026, 12, 31))
        self.assertIs(decision.field_status("address"), S.NORMAL)
        self.assertIs(decision.field_status("notes"), S.NORMAL)
        self.assertIs(decision.field_status("ssn"), S.PRESERVED)

    def test_on_expiry_day_remaining_columns_expire_but_held_ones_remain(self) -> None:
        decision = self.engine.decide("case-1", date(2027, 1, 1))
        self.assertIs(decision.field_status("ssn"), S.PRESERVED)
        self.assertIs(decision.field_status("name"), S.PRESERVED)
        self.assertIs(decision.field_status("address"), S.EXPIRED)
        self.assertIs(decision.field_status("notes"), S.EXPIRED)
        # Controlled view still contains only the held two columns.
        self.assertEqual(
            set(decision.controlled_view_fields), {"name", "ssn"}
        )
        # Everything non-normal is withdrawn from the ordinary view.
        self.assertEqual(
            set(decision.withdrawn_fields),
            {"name", "ssn", "address", "notes"},
        )
        self.assertEqual(decision.normal_view_fields, ())

    def test_long_after_expiry_hold_keeps_only_named_columns(self) -> None:
        decision = self.engine.decide("case-1", date(2030, 1, 1))
        statuses = {d.field_name: d.status for d in decision.field_decisions}
        self.assertEqual(
            statuses,
            {
                "name": S.PRESERVED,
                "ssn": S.PRESERVED,
                "address": S.EXPIRED,
                "notes": S.EXPIRED,
            },
        )

    def test_views_are_disjoint_and_sorted(self) -> None:
        decision = self.engine.decide("case-1", date(2027, 6, 1))
        self.assertEqual(
            decision.controlled_view_fields,
            tuple(sorted(decision.controlled_view_fields)),
        )
        self.assertEqual(
            decision.withdrawn_fields,
            tuple(sorted(decision.withdrawn_fields)),
        )
        self.assertEqual(
            set(decision.controlled_view_fields)
            & set(decision.normal_view_fields),
            set(),
        )
        self.assertEqual(
            set(decision.controlled_view_fields)
            | set(f for f in decision.withdrawn_fields
                  if f not in decision.controlled_view_fields),
            set(decision.withdrawn_fields),
        )


if __name__ == "__main__":
    unittest.main()
