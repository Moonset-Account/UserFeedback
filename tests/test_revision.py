import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from disclosure_review import (
    CarryForward,
    MaterialRegistry,
    RecordRef,
    ReviewEngine,
    ReviewPolicy,
    VersionedRecord,
)


class RevisionCarryForwardTests(unittest.TestCase):
    def setUp(self):
        self.old_ref = RecordRef("statement-rev", "1")
        self.new_ref = RecordRef("statement-rev", "2")
        self.old = VersionedRecord(
            ref=self.old_ref,
            paragraphs=[
                "This first paragraph remains exactly unchanged.",
                "This is the old second paragraph.",
            ],
        )
        self.new = VersionedRecord(
            ref=self.new_ref,
            paragraphs=[
                "This first paragraph remains exactly unchanged.",
                "This second paragraph has been rewritten.",
                "This is a brand new third paragraph.",
            ],
        )
        self.registry = MaterialRegistry([self.old, self.new])
        self.registry.approve(self.old_ref, [1, 2], approval_id="round-1-review")

    def test_declared_verbatim_paragraph_inherits_approval(self):
        self.registry.add_carry_forward(
            CarryForward(
                target=self.new_ref,
                source=self.old_ref,
                paragraph_map={1: 1},
                declaration_id="carry-p1",
            )
        )
        engine = ReviewEngine(self.registry)
        engine.baseline_submission = self.old_ref
        report = engine.review_current(self.new)

        self.assertEqual(report.status_for(1), "disclosable")
        self.assertEqual(report.status_for(2), "withheld")
        self.assertEqual(report.status_for(3), "withheld")
        self.assertEqual(report.disclosable_ranges, [[1, 1]])
        self.assertIn("REWRITTEN_NEEDS_CONFIRMATION", report.reason_codes_for(2))
        self.assertIn("REWRITTEN_NEEDS_CONFIRMATION", report.reason_codes_for(3))

    def test_identical_but_undeclared_paragraph_still_needs_declaration(self):
        engine = ReviewEngine(self.registry)
        engine.baseline_submission = self.old_ref
        report = engine.review_current(self.new)
        self.assertEqual(report.status_for(1), "withheld")
        self.assertIn(
            "CARRY_FORWARD_DECLARATION_REQUIRED", report.reason_codes_for(1)
        )

    def test_false_verbatim_declaration_is_detected_by_hash(self):
        self.registry.add_carry_forward(
            CarryForward(
                target=self.new_ref,
                source=self.old_ref,
                paragraph_map={1: 1, 2: 2},
                declaration_id="bad-carry",
            )
        )
        report = ReviewEngine(self.registry).review_current(self.new)
        self.assertEqual(report.status_for(1), "disclosable")
        self.assertEqual(report.status_for(2), "withheld")
        self.assertIn("REWRITTEN_NEEDS_CONFIRMATION", report.reason_codes_for(2))

    def test_fresh_confirmation_releases_rewritten_paragraph(self):
        self.registry.add_carry_forward(
            CarryForward(
                target=self.new_ref,
                source=self.old_ref,
                paragraph_map={1: 1},
            )
        )
        self.registry.approve(self.new_ref, [3], approval_id="round-2-p3")
        engine = ReviewEngine(self.registry)
        engine.baseline_submission = self.old_ref
        report = engine.review_current(self.new)

        self.assertEqual(report.status_for(1), "disclosable")
        self.assertEqual(report.status_for(2), "withheld")
        self.assertEqual(report.status_for(3), "disclosable")
        self.assertEqual(report.disclosable_ranges, [[1, 1], [3, 3]])

    def test_missing_carry_source_points_to_baseline_record(self):
        missing_source = RecordRef("statement-rev", "0")
        self.registry.add_carry_forward(
            CarryForward(
                target=self.new_ref,
                source=missing_source,
                paragraph_map={1: 1},
            )
        )
        report = ReviewEngine(self.registry).review_current(self.new)
        self.assertEqual(report.status_for(1), "withheld")
        codes = report.reason_codes_for(1)
        self.assertIn("CARRY_FORWARD_SOURCE_MISSING", codes)
        remediation = next(
            item
            for item in report.remediations
            if item.code == "CARRY_FORWARD_SOURCE_MISSING"
        )
        self.assertEqual(remediation.repair_from.version, "0")

    def test_multi_hop_carry_chain_reaches_original_approval(self):
        v1_ref = RecordRef("multi", "1")
        v2_ref = RecordRef("multi", "2")
        v3_ref = RecordRef("multi", "3")
        v1 = VersionedRecord(ref=v1_ref, paragraphs=["Stable approved paragraph."])
        v2 = VersionedRecord(ref=v2_ref, paragraphs=["Stable approved paragraph."])
        v3 = VersionedRecord(ref=v3_ref, paragraphs=["Stable approved paragraph."])
        registry = MaterialRegistry([v1, v2, v3])
        registry.approve(v1_ref, [1])
        registry.add_carry_forward(CarryForward(v2_ref, v1_ref, {1: 1}, "v2-carry"))
        registry.add_carry_forward(CarryForward(v3_ref, v2_ref, {1: 1}, "v3-carry"))

        report = ReviewEngine(registry).review_current(v3)
        self.assertEqual(report.status_for(1), "disclosable")
        nodes = {(n.record_id, n.version) for n in report.paragraph_results[0].source_nodes()}
        self.assertIn(("multi", "1"), nodes)
        self.assertIn(("multi", "2"), nodes)

    def test_optional_policy_allows_implicit_same_number_match(self):
        engine = ReviewEngine(
            self.registry,
            policy=ReviewPolicy(require_carry_declaration_for_revision=False),
        )
        engine.baseline_submission = self.old_ref
        report = engine.review_current(self.new)
        self.assertEqual(report.status_for(1), "disclosable")
        self.assertEqual(report.status_for(2), "withheld")
        self.assertIn("REWRITTEN_NEEDS_CONFIRMATION", report.reason_codes_for(2))

    def test_missing_baseline_record_gives_one_repair_starting_point(self):
        missing_baseline = RecordRef("statement-rev", "99")
        engine = ReviewEngine(self.registry)
        engine.baseline_submission = missing_baseline
        report = engine.review_current(self.new)

        self.assertEqual(report.disclosable_paragraphs(), [])
        self.assertTrue(
            all("BASELINE_RECORD_MISSING" in report.reason_codes_for(i) for i in (1, 2, 3))
        )
        remediation = next(
            item for item in report.remediations if item.code == "BASELINE_RECORD_MISSING"
        )
        self.assertEqual(remediation.repair_from, missing_baseline)
        self.assertEqual(remediation.affected_submission_paragraphs, [1, 2, 3])

    def test_carry_forward_cycle_is_detected(self):
        v1_ref = RecordRef("cyclic-doc", "1")
        v2_ref = RecordRef("cyclic-doc", "2")
        v1 = VersionedRecord(ref=v1_ref, paragraphs=["Same text in v1."])
        v2 = VersionedRecord(ref=v2_ref, paragraphs=["Same text in v1."])
        registry = MaterialRegistry([v1, v2])
        registry.add_carry_forward(CarryForward(v2_ref, v1_ref, {1: 1}, "v2-to-v1"))
        registry.add_carry_forward(CarryForward(v1_ref, v2_ref, {1: 1}, "v1-to-v2"))

        report = ReviewEngine(registry).review_current(v2)
        self.assertEqual(report.status_for(1), "withheld")
        self.assertIn("CARRY_FORWARD_CYCLE", report.reason_codes_for(1))
        remediation = next(
            item for item in report.remediations if item.code == "CARRY_FORWARD_CYCLE"
        )
        self.assertEqual(remediation.repair_from.version, "2")


if __name__ == "__main__":
    unittest.main()
