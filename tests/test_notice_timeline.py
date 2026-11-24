"""Notice timing: effective boundaries, expiry, overlaps and corrections."""

import unittest
from datetime import date

from retention_decisions import (
    DerivedArtifact,
    Material,
    PreservationNotice,
    RetentionEngine,
    RetentionStatus as S,
)


class NoticeTimingTest(unittest.TestCase):
    def _engine(self) -> RetentionEngine:
        engine = RetentionEngine()
        engine.register_material(
            Material.create(
                "m", ["a", "b", "c"], date(2027, 1, 1)
            )
        )
        return engine

    def test_notice_is_inactive_before_its_effective_date(self) -> None:
        engine = self._engine()
        engine.register_notice(
            PreservationNotice.create("N1", "m", ["a"], date(2026, 6, 1))
        )
        self.assertIs(
            engine.decide("m", date(2026, 5, 31)).field_status("a"),
            S.NORMAL,
        )
        self.assertIs(
            engine.decide("m", date(2026, 6, 1)).field_status("a"),
            S.PRESERVED,
        )

    def test_notice_with_expiry_uses_half_open_interval(self) -> None:
        engine = self._engine()
        # Short hold: effective 2026-03-01 through 2026-08-31 inclusive.
        engine.register_notice(
            PreservationNotice.create(
                "N1", "m", ["a"],
                date(2026, 3, 1), expires_on=date(2026, 9, 1),
            )
        )
        # Before the hold.
        self.assertIs(
            engine.decide("m", date(2026, 2, 28)).field_status("a"),
            S.NORMAL,
        )
        # First and last effective days.
        self.assertIs(
            engine.decide("m", date(2026, 3, 1)).field_status("a"),
            S.PRESERVED,
        )
        self.assertIs(
            engine.decide("m", date(2026, 8, 31)).field_status("a"),
            S.PRESERVED,
        )
        # expires_on itself: hold gone, but the ordinary period still runs.
        decision = engine.decide("m", date(2026, 9, 1))
        self.assertIs(decision.field_status("a"), S.NORMAL)
        self.assertEqual(
            decision.field_decisions[0].active_notice_ids, ()
        )
        # After the ordinary expiry the lapsed hold no longer protects "a".
        self.assertIs(
            engine.decide("m", date(2027, 1, 1)).field_status("a"),
            S.EXPIRED,
        )

    def test_overlapping_notices_both_report_on_held_field(self) -> None:
        engine = self._engine()
        engine.register_notice(
            PreservationNotice.create("N1", "m", ["a"], date(2026, 1, 1))
        )
        engine.register_notice(
            PreservationNotice.create("N2", "m", ["a", "b"], date(2026, 5, 1))
        )
        decision = engine.decide("m", date(2026, 6, 1))
        a = next(d for d in decision.field_decisions if d.field_name == "a")
        b = next(d for d in decision.field_decisions if d.field_name == "b")
        self.assertEqual(a.active_notice_ids, ("N1", "N2"))
        self.assertEqual(b.active_notice_ids, ("N2",))
        self.assertIs(a.status, S.PRESERVED)
        self.assertIs(b.status, S.PRESERVED)
        self.assertIs(decision.field_status("c"), S.NORMAL)

    def test_registration_order_does_not_change_adjudication(self) -> None:
        def build(reversed_order: bool) -> RetentionEngine:
            engine = self._engine()
            notices = [
                PreservationNotice.create(
                    "N1", "m", ["a"], date(2026, 1, 1)
                ),
                PreservationNotice.create(
                    "N2", "m", ["a", "b"],
                    date(2026, 5, 1), expires_on=date(2026, 9, 1),
                ),
            ]
            for notice in (reversed(notices) if reversed_order else notices):
                engine.register_notice(notice)
            return engine

        for day in (date(2026, 4, 1), date(2026, 6, 1), date(2026, 10, 1)):
            left = build(False).decide("m", day)
            right = build(True).decide("m", day)
            self.assertEqual(
                tuple((d.field_name, d.status, d.active_notice_ids)
                      for d in left.field_decisions),
                tuple((d.field_name, d.status, d.active_notice_ids)
                      for d in right.field_decisions),
            )

    def test_notice_scope_partial_columns_leave_others_on_schedule(self) -> None:
        engine = self._engine()
        engine.register_notice(
            PreservationNotice.create(
                "N1", "m", ["a", "b"], date(2026, 6, 1)
            )
        )
        decision = engine.decide("m", date(2028, 1, 1))
        self.assertIs(decision.field_status("a"), S.PRESERVED)
        self.assertIs(decision.field_status("b"), S.PRESERVED)
        # The uncovered third column simply aged out.
        self.assertIs(decision.field_status("c"), S.EXPIRED)
        self.assertEqual(
            set(decision.controlled_view_fields), {"a", "b"}
        )

    def test_replacement_correction_narrows_hold_from_own_effective_date(self) -> None:
        engine = self._engine()
        # Original broad hold.
        engine.register_notice(
            PreservationNotice.create(
                "N1", "m", ["a", "b", "c"], date(2026, 2, 1)
            )
        )
        # Correction arriving later, effective 2026-07-01: only "a" stays
        # held; from that date the correction supersedes N1.
        engine.register_notice(
            PreservationNotice.create(
                "N2", "m", ["a"], date(2026, 7, 1),
                amends_notice_id="N1",
            )
        )
        # Before the correction all three columns are held by N1.
        before = engine.decide("m", date(2026, 6, 30))
        self.assertEqual(
            set(before.controlled_view_fields), {"a", "b", "c"}
        )
        for field in before.field_decisions:
            self.assertEqual(field.active_notice_ids, ("N1",))

        # From the correction's effective date, b/c are no longer covered.
        after = engine.decide("m", date(2026, 7, 1))
        self.assertIs(after.field_status("a"), S.PRESERVED)
        self.assertEqual(
            next(d for d in after.field_decisions
                 if d.field_name == "a").active_notice_ids,
            ("N2",),
        )
        # N1 is superseded on this day and must not appear as an active notice.
        self.assertNotIn(
            "N1",
            [notice_id for d in after.field_decisions
             for notice_id in d.active_notice_ids],
        )
        # Still inside the ordinary period, so they return to the normal view.
        self.assertIs(after.field_status("b"), S.NORMAL)
        self.assertIs(after.field_status("c"), S.NORMAL)
        self.assertEqual(after.controlled_view_fields, ("a",))

        # After ordinary expiry, the dropped columns expire while "a" remains.
        later = engine.decide("m", date(2027, 6, 1))
        self.assertIs(later.field_status("a"), S.PRESERVED)
        self.assertIs(later.field_status("b"), S.EXPIRED)
        self.assertIs(later.field_status("c"), S.EXPIRED)

    def test_lapsed_correction_releases_the_original_notice(self) -> None:
        engine = self._engine()
        engine.register_notice(
            PreservationNotice.create(
                "N1", "m", ["a", "b"], date(2026, 1, 1)
            )
        )
        # Temporary correction with its own end date.
        engine.register_notice(
            PreservationNotice.create(
                "N2", "m", ["a"], date(2026, 6, 1),
                expires_on=date(2026, 9, 1), amends_notice_id="N1",
            )
        )
        during = engine.decide("m", date(2026, 7, 1))
        self.assertEqual(during.controlled_view_fields, ("a",))
        self.assertIs(during.field_status("b"), S.NORMAL)
        # Once the correction lapses, N1's full scope applies again.
        after = engine.decide("m", date(2026, 9, 1))
        self.assertEqual(
            set(after.controlled_view_fields), {"a", "b"}
        )
        self.assertEqual(
            next(d for d in after.field_decisions
                 if d.field_name == "b").active_notice_ids,
            ("N1",),
        )


class HistoricalReplayTest(unittest.TestCase):
    def _engine(self) -> RetentionEngine:
        engine = RetentionEngine()
        engine.register_material(
            Material.create(
                "m", ["a", "b"], date(2027, 1, 1)
            )
        )
        engine.register_artifact(
            DerivedArtifact.create("art", "m", ["a", "b"])
        )
        return engine

    def test_replay_uses_only_then_effective_notices(self) -> None:
        engine = self._engine()
        engine.register_notice(
            PreservationNotice.create(
                "N1", "m", ["a"], date(2026, 1, 1)
            )
        )
        # A correction registered "today" (2026-11) but effective 2026-10-01.
        engine.register_notice(
            PreservationNotice.create(
                "N2", "m", ["b"], date(2026, 10, 1)
            )
        )
        # Replaying a day before N2's effective date: N2 must not apply.
        replay = engine.decide("m", date(2026, 9, 1))
        self.assertIs(replay.field_status("a"), S.PRESERVED)
        self.assertIs(replay.field_status("b"), S.NORMAL)
        self.assertEqual(
            next(d for d in replay.field_decisions
                 if d.field_name == "b").active_notice_ids,
            (),
        )
        # Replaying on/after N2's effective date: the correction applies even
        # though it physically arrived later.
        replayed_later = engine.decide("m", date(2026, 10, 1))
        self.assertIs(replayed_later.field_status("b"), S.PRESERVED)
        self.assertEqual(
            next(d for d in replayed_later.field_decisions
                 if d.field_name == "b").active_notice_ids,
            ("N2",),
        )

    def test_future_dated_correction_does_not_affect_today(self) -> None:
        engine = self._engine()
        engine.register_notice(
            PreservationNotice.create(
                "N1", "m", ["a", "b"], date(2026, 1, 1)
            )
        )
        # Correction filed now but only effective in the future.
        engine.register_notice(
            PreservationNotice.create(
                "N2", "m", ["a"], date(2027, 6, 1),
                amends_notice_id="N1",
            )
        )
        today = engine.decide("m", date(2026, 11, 25))
        self.assertEqual(
            set(today.controlled_view_fields), {"a", "b"}
        )
        future = engine.decide("m", date(2027, 6, 1))
        self.assertEqual(future.controlled_view_fields, ("a",))
        self.assertIs(future.field_status("b"), S.EXPIRED)

    def test_correction_reaches_back_only_to_its_own_effective_date(self) -> None:
        engine = self._engine()
        engine.register_notice(
            PreservationNotice.create(
                "N1", "m", ["a", "b"], date(2026, 1, 1)
            )
        )
        # Correction "arrives" in November but is backdated to 2026-10-01.
        engine.register_notice(
            PreservationNotice.create(
                "N2", "m", ["a"], date(2026, 10, 1),
                amends_notice_id="N1",
            )
        )
        # Replay before the correction's effective date: N1 still governs.
        old_view = engine.decide("m", date(2026, 9, 30))
        self.assertEqual(
            set(old_view.controlled_view_fields), {"a", "b"}
        )
        self.assertTrue(
            all(
                d.active_notice_ids == ("N1",)
                for d in old_view.field_decisions
                if d.status is S.PRESERVED
            )
        )
        # Replay on/after the effective date: the later-arriving correction
        # does influence the historical view, exactly per its effective date.
        corrected_view = engine.decide("m", date(2026, 10, 1))
        self.assertEqual(corrected_view.controlled_view_fields, ("a",))
        self.assertIs(corrected_view.field_status("b"), S.NORMAL)
        self.assertEqual(
            next(d for d in corrected_view.field_decisions
                 if d.field_name == "a").active_notice_ids,
            ("N2",),
        )

    def test_replay_is_deterministic_across_calls(self) -> None:
        engine = self._engine()
        engine.register_notice(
            PreservationNotice.create(
                "N1", "m", ["a"], date(2026, 3, 1),
                expires_on=date(2026, 8, 1),
            )
        )
        first = engine.decide("m", date(2026, 5, 1))
        second = engine.decide("m", date(2026, 5, 1))
        self.assertEqual(first, second)
        # Artifact replay reflects the same effective notice set.
        self.assertIs(first.artifact_status("art"), S.PRESERVED)
        self.assertEqual(
            next(iter(first.artifact_decisions)).holding_notice_ids,
            ("N1",),
        )


if __name__ == "__main__":
    unittest.main()
