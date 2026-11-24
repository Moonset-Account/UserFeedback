"""Derived-artifact adjudication and reference relationship reporting."""

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


class ArtifactDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = build_engine()

    def test_artifact_preserved_when_one_referenced_field_is_held(self) -> None:
        decision = self.engine.decide("case-1", date(2026, 6, 1))
        # A-summary cites name (held) + address (normal): the hold wins.
        self.assertIs(decision.artifact_status("A-summary"), S.PRESERVED)
        self.assertIs(decision.artifact_status("A-ssn"), S.PRESERVED)
        self.assertIs(decision.artifact_status("A-full"), S.PRESERVED)
        # A-addr only cites a normal field: stays in the ordinary view.
        self.assertIs(decision.artifact_status("A-addr"), S.NORMAL)
        self.assertEqual(
            set(decision.controlled_view_artifacts),
            {"A-summary", "A-ssn", "A-full"},
        )
        self.assertEqual(
            set(decision.withdrawn_artifacts),
            {"A-summary", "A-ssn", "A-full"},
        )
        self.assertEqual(decision.normal_view_artifacts, ("A-addr",))

    def test_holding_notice_ids_are_reported_per_artifact(self) -> None:
        decision = self.engine.decide("case-1", date(2027, 6, 1))
        by_id = {d.artifact_id: d for d in decision.artifact_decisions}
        self.assertEqual(by_id["A-ssn"].holding_notice_ids, ("N-1",))
        self.assertEqual(by_id["A-summary"].holding_notice_ids, ("N-1",))
        self.assertEqual(by_id["A-full"].holding_notice_ids, ("N-1",))
        self.assertEqual(by_id["A-addr"].holding_notice_ids, ())

    def test_artifact_expires_when_all_supporting_fields_age_out(self) -> None:
        decision = self.engine.decide("case-1", date(2027, 6, 1))
        # address alone expired -> A-addr loses all support.
        self.assertIs(decision.artifact_status("A-addr"), S.EXPIRED)
        # Held artifacts survive; none are merely EXPIRED while a hold exists.
        self.assertIs(decision.artifact_status("A-ssn"), S.PRESERVED)
        self.assertEqual(
            set(decision.withdrawn_artifacts),
            {"A-summary", "A-ssn", "A-full", "A-addr"},
        )
        self.assertEqual(decision.normal_view_artifacts, ())

    def test_reference_links_carry_per_field_status(self) -> None:
        decision = self.engine.decide("case-1", date(2027, 6, 1))
        links = {
            (link.artifact_id, link.field_name): link.field_status
            for link in decision.reference_links
        }
        self.assertEqual(
            links,
            {
                ("A-summary", "address"): S.EXPIRED,
                ("A-summary", "name"): S.PRESERVED,
                ("A-ssn", "ssn"): S.PRESERVED,
                ("A-full", "address"): S.EXPIRED,
                ("A-full", "name"): S.PRESERVED,
                ("A-full", "notes"): S.EXPIRED,
                ("A-full", "ssn"): S.PRESERVED,
                ("A-addr", "address"): S.EXPIRED,
            },
        )
        # Every link points back at the adjudicated material.
        self.assertTrue(
            all(link.material_id == "case-1"
                for link in decision.reference_links)
        )
        # One edge per (artifact, referenced field) pair.
        self.assertEqual(
            len(decision.reference_links), 2 + 1 + 4 + 1
        )

    def test_reference_links_are_also_nested_on_artifact_decisions(self) -> None:
        decision = self.engine.decide("case-1", date(2027, 1, 1))
        full = next(
            d for d in decision.artifact_decisions
            if d.artifact_id == "A-full"
        )
        self.assertEqual(
            full.referenced_fields, ("address", "name", "notes", "ssn")
        )
        nested = {(l.field_name, l.field_status) for l in full.references}
        self.assertEqual(
            nested,
            {
                ("address", S.EXPIRED),
                ("name", S.PRESERVED),
                ("notes", S.EXPIRED),
                ("ssn", S.PRESERVED),
            },
        )

    def test_artifact_without_any_referenced_field_is_supported(self) -> None:
        engine = RetentionEngine()
        engine.register_material(
            Material.create("m", ["f1"], date(2027, 1, 1))
        )
        engine.register_artifact(
            DerivedArtifact.create("empty", "m", [])
        )
        before = engine.decide("m", date(2026, 1, 1))
        after = engine.decide("m", date(2028, 1, 1))
        # No cited fields: nothing can pull it into a hold, and nothing ages
        # out underneath it either, so it never needs to be withdrawn.
        self.assertIs(before.artifact_status("empty"), S.NORMAL)
        self.assertIs(after.artifact_status("empty"), S.NORMAL)


if __name__ == "__main__":
    unittest.main()
