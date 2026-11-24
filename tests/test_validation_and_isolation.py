"""Input validation, material isolation and the one-shot helper."""

import unittest
from datetime import date

from retention_decisions import (
    DerivedArtifact,
    Material,
    PreservationNotice,
    RetentionEngine,
    RetentionStatus as S,
    decide_retention,
)


class ValidationTest(unittest.TestCase):
    def test_material_requires_fields_and_id(self) -> None:
        with self.assertRaises(ValueError):
            Material.create("", ["a"], date(2027, 1, 1))
        with self.assertRaises(ValueError):
            Material.create("m", [], date(2027, 1, 1))
        # Duplicates reach the validator only through the raw constructor;
        # create() normalises via a set before construction.
        with self.assertRaises(ValueError):
            Material(
                material_id="m",
                fields=("a", "a"),
                normal_expiry=date(2027, 1, 1),
            )
        with self.assertRaises(ValueError):
            Material(
                material_id="m",
                fields=("a", ""),
                normal_expiry=date(2027, 1, 1),
            )

    def test_notice_requires_nonempty_hold_and_valid_interval(self) -> None:
        with self.assertRaises(ValueError):
            PreservationNotice.create(
                "n", "m", [], date(2026, 1, 1)
            )
        with self.assertRaises(ValueError):
            PreservationNotice.create(
                "n", "m", ["a"],
                date(2026, 1, 1), expires_on=date(2026, 1, 1),
            )
        with self.assertRaises(ValueError):
            PreservationNotice.create(
                "n", "m", ["a"],
                date(2026, 1, 2), expires_on=date(2026, 1, 1),
            )
        with self.assertRaises(ValueError):
            PreservationNotice(
                notice_id="n",
                material_id="m",
                fields=("a", "a"),
                effective_date=date(2026, 1, 1),
            )
        with self.assertRaises(ValueError):
            PreservationNotice.create(
                "n", "m", ["a"], date(2026, 1, 1),
                amends_notice_id="n",
            )

    def test_artifact_duplicate_field_rejected_via_raw_constructor(self) -> None:
        with self.assertRaises(ValueError):
            DerivedArtifact(
                artifact_id="x",
                material_id="m",
                referenced_fields=("a", "a"),
            )

    def test_register_notice_rejects_unknown_material_or_field(self) -> None:
        engine = RetentionEngine()
        engine.register_material(
            Material.create("m", ["a"], date(2027, 1, 1))
        )
        with self.assertRaises(ValueError):
            engine.register_notice(
                PreservationNotice.create(
                    "n", "other", ["a"], date(2026, 1, 1)
                )
            )
        with self.assertRaises(ValueError):
            engine.register_notice(
                PreservationNotice.create(
                    "n", "m", ["ghost"], date(2026, 1, 1)
                )
            )

    def test_amendment_must_target_an_existing_notice_on_same_material(self) -> None:
        engine = RetentionEngine()
        engine.register_material(
            Material.create("m", ["a", "b"], date(2027, 1, 1))
        )
        engine.register_material(
            Material.create("other", ["a"], date(2027, 1, 1))
        )
        engine.register_notice(
            PreservationNotice.create("n1", "m", ["a"], date(2026, 1, 1))
        )
        with self.assertRaises(ValueError):
            engine.register_notice(
                PreservationNotice.create(
                    "n2", "m", ["b"], date(2026, 2, 1),
                    amends_notice_id="missing",
                )
            )
        with self.assertRaises(ValueError):
            engine.register_notice(
                PreservationNotice.create(
                    "n3", "m", ["b"], date(2026, 2, 1),
                    amends_notice_id="n3",
                )
            )
        other_material_notice = PreservationNotice.create(
            "n4", "other", ["a"], date(2026, 1, 1)
        )
        engine.register_notice(other_material_notice)
        with self.assertRaises(ValueError):
            engine.register_notice(
                PreservationNotice.create(
                    "n5", "m", ["b"], date(2026, 2, 1),
                    amends_notice_id="n4",
                )
            )

    def test_register_artifact_rejects_unknown_references(self) -> None:
        engine = RetentionEngine()
        engine.register_material(
            Material.create("m", ["a"], date(2027, 1, 1))
        )
        with self.assertRaises(ValueError):
            engine.register_artifact(
                DerivedArtifact.create("x", "other", ["a"])
            )
        with self.assertRaises(ValueError):
            engine.register_artifact(
                DerivedArtifact.create("x", "m", ["a", "ghost"])
            )

    def test_duplicate_registration_is_rejected(self) -> None:
        engine = RetentionEngine()
        engine.register_material(
            Material.create("m", ["a"], date(2027, 1, 1))
        )
        with self.assertRaises(ValueError):
            engine.register_material(
                Material.create("m", ["b"], date(2028, 1, 1))
            )
        engine.register_notice(
            PreservationNotice.create("n", "m", ["a"], date(2026, 1, 1))
        )
        with self.assertRaises(ValueError):
            engine.register_notice(
                PreservationNotice.create("n", "m", ["a"], date(2026, 2, 1))
            )
        engine.register_artifact(DerivedArtifact.create("x", "m", ["a"]))
        with self.assertRaises(ValueError):
            engine.register_artifact(DerivedArtifact.create("x", "m", ["a"]))

    def test_decide_unknown_material_and_bad_day_type(self) -> None:
        engine = RetentionEngine()
        with self.assertRaises(KeyError):
            engine.decide("missing", date(2026, 1, 1))
        engine.register_material(
            Material.create("m", ["a"], date(2027, 1, 1))
        )
        with self.assertRaises(TypeError):
            engine.decide("m", "2026-01-01")  # type: ignore[arg-type]

    def test_frozen_models_reject_mutation(self) -> None:
        material = Material.create("m", ["a"], date(2027, 1, 1))
        with self.assertRaises(AttributeError):
            material.material_id = "other"  # type: ignore[misc]
        notice = PreservationNotice.create(
            "n", "m", ["a"], date(2026, 1, 1)
        )
        self.assertTrue(notice.is_effective_on(date(2026, 1, 1)))
        self.assertFalse(notice.is_effective_on(date(2025, 12, 31)))


class MaterialIsolationTest(unittest.TestCase):
    def test_notices_and_artifacts_are_scoped_per_material(self) -> None:
        engine = RetentionEngine()
        engine.register_material(
            Material.create("m1", ["a", "b"], date(2027, 1, 1))
        )
        engine.register_material(
            Material.create("m2", ["a", "b"], date(2028, 1, 1))
        )
        engine.register_notice(
            PreservationNotice.create(
                "n-m1", "m1", ["a"], date(2026, 1, 1)
            )
        )
        engine.register_artifact(
            DerivedArtifact.create("art-1", "m1", ["a"])
        )
        engine.register_artifact(
            DerivedArtifact.create("art-2", "m2", ["a"])
        )

        day = date(2027, 6, 1)
        d1 = engine.decide("m1", day)
        d2 = engine.decide("m2", day)

        self.assertIs(d1.field_status("a"), S.PRESERVED)
        self.assertIs(d1.field_status("b"), S.EXPIRED)
        # m2 has no notice: "a" ages normally, and is still inside its period.
        self.assertIs(d2.field_status("a"), S.NORMAL)
        self.assertIs(d2.field_status("b"), S.NORMAL)
        self.assertEqual(
            [d.artifact_id for d in d1.artifact_decisions], ["art-1"]
        )
        self.assertEqual(
            [d.artifact_id for d in d2.artifact_decisions], ["art-2"]
        )
        self.assertIs(d1.artifact_status("art-1"), S.PRESERVED)
        self.assertIs(d2.artifact_status("art-2"), S.NORMAL)
        # Reference links never leak across materials.
        self.assertTrue(
            all(link.material_id == "m1" for link in d1.reference_links)
        )
        self.assertTrue(
            all(link.material_id == "m2" for link in d2.reference_links)
        )


class ConvenienceHelperTest(unittest.TestCase):
    def test_decide_retention_one_shot(self) -> None:
        materials = [
            Material.create("m", ["a", "b"], date(2027, 1, 1))
        ]
        notices = [
            PreservationNotice.create(
                "n", "m", ["a"], date(2026, 6, 1)
            )
        ]
        artifacts = [
            DerivedArtifact.create("art", "m", ["a", "b"])
        ]
        decision = decide_retention(
            materials, notices, artifacts, "m", date(2028, 1, 1)
        )
        self.assertEqual(decision.material_id, "m")
        self.assertEqual(decision.as_of, date(2028, 1, 1))
        self.assertIs(decision.field_status("a"), S.PRESERVED)
        self.assertIs(decision.field_status("b"), S.EXPIRED)
        self.assertIs(decision.artifact_status("art"), S.PRESERVED)
        self.assertEqual(
            next(iter(decision.artifact_decisions)).holding_notice_ids,
            ("n",),
        )


if __name__ == "__main__":
    unittest.main()
