import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from disclosure_review import (
    Citation,
    MaterialRegistry,
    Range,
    RecordRef,
    ReviewEngine,
    VersionedRecord,
)


class NestedCitationTests(unittest.TestCase):
    def _case(self, *, approve_inner: bool):
        outer_ref = RecordRef("interview-b", "2")
        inner_ref = RecordRef("memo-x", "1")
        outer = VersionedRecord.create(
            "interview-b",
            "2",
            [
                "Outer paragraph one.",
                "Outer paragraph two quotes memo-x.",
                "Outer paragraph three.",
            ],
            citations=[
                Citation("outer-to-memo", Range(2, 2), inner_ref, Range(1, 1))
            ],
        )
        inner = VersionedRecord.create(
            "memo-x", "1", ["Inner memo paragraph requiring separate approval."]
        )
        submission = VersionedRecord.create(
            "statement-nested",
            "1",
            [
                "Intro.",
                "Submission copy of outer p1.",
                "Submission copy of outer p2 with nested excerpt.",
                "Submission copy of outer p3.",
                "Closing.",
            ],
            citations=[
                Citation("statement-to-outer", Range(2, 4), outer_ref, Range(1, 3))
            ],
        )
        registry = MaterialRegistry([outer, inner, submission])
        registry.approve(outer_ref, [Range(1, 3)], approval_id="outer-approval")
        if approve_inner:
            registry.approve(inner_ref, [1], approval_id="inner-approval")
        return registry, submission

    def test_outer_approval_does_not_cover_unapproved_inner_source(self):
        registry, submission = self._case(approve_inner=False)
        report = ReviewEngine(registry).review_current(submission)

        self.assertEqual(report.disclosable_ranges, [[1, 2], [4, 5]])
        self.assertEqual([segment.paragraph_range for segment in report.withheld_segments], [Range(3, 3)])
        codes = report.reason_codes_for(3)
        self.assertIn("NOT_IN_APPROVED_SCOPE", codes)

        source_nodes = report.paragraph_results[2].source_nodes()
        node_ids = {(node.record_id, node.version, node.paragraph) for node in source_nodes}
        self.assertIn(("memo-x", "1", 1), node_ids)
        self.assertIn(("interview-b", "2", 2), node_ids)

    def test_nested_source_can_be_released_after_separate_approval(self):
        registry, submission = self._case(approve_inner=True)
        report = ReviewEngine(registry).review_current(submission)
        self.assertEqual(report.withheld_paragraphs(), [])
        self.assertEqual(report.disclosable_ranges, [[1, 5]])


class MissingAndCycleTests(unittest.TestCase):
    def test_missing_material_points_to_exact_record_to_supply(self):
        submission = VersionedRecord.create(
            "statement-missing",
            "1",
            ["Before.", "Quote from absent source.", "After."],
            citations=[
                Citation(
                    "missing-cite",
                    Range(2, 2),
                    RecordRef("absent-material", "7"),
                    Range(3, 4),
                )
            ],
        )
        report = ReviewEngine(MaterialRegistry([submission])).review_current(submission)
        self.assertEqual(report.status_for(2), "withheld")
        self.assertIn("MATERIAL_NOT_FOUND", report.reason_codes_for(2))
        remediation = report.remediations[0]
        self.assertEqual(remediation.code, "MATERIAL_NOT_FOUND")
        self.assertEqual(remediation.repair_from.record_id, "absent-material")
        self.assertEqual(remediation.repair_from.version, "7")
        self.assertIn(2, remediation.affected_submission_paragraphs)
        self.assertTrue(remediation.action)

    def test_missing_version_lists_available_versions(self):
        existing = VersionedRecord.create("versioned-doc", "1", ["Old version text."])
        submission = VersionedRecord.create(
            "statement-version",
            "1",
            ["Quote from wrong version."],
            citations=[
                Citation(
                    "bad-version",
                    Range(1, 1),
                    RecordRef("versioned-doc", "2"),
                    Range(1, 1),
                )
            ],
        )
        registry = MaterialRegistry([existing, submission])
        report = ReviewEngine(registry).review_current(submission)
        self.assertIn("VERSION_NOT_FOUND", report.reason_codes_for(1))
        remediation = next(r for r in report.remediations if r.code == "VERSION_NOT_FOUND")
        self.assertEqual(remediation.repair_from.version, "2")
        self.assertEqual(remediation.detail["available_versions"], ["1"])

    def test_nested_missing_source_reports_where_to_start_repair(self):
        outer_ref = RecordRef("outer-present", "1")
        outer = VersionedRecord.create(
            "outer-present",
            "1",
            ["Outer paragraph with missing nested source."],
            citations=[
                Citation(
                    "nested-missing",
                    Range(1, 1),
                    RecordRef("inner-missing", "3"),
                    Range(2, 2),
                )
            ],
        )
        submission = VersionedRecord.create(
            "statement-nested-missing",
            "1",
            ["Submitted excerpt."],
            citations=[Citation("to-outer", Range(1, 1), outer_ref, Range(1, 1))],
        )
        registry = MaterialRegistry([outer, submission])
        registry.approve(outer_ref, [1])
        report = ReviewEngine(registry).review_current(submission)

        self.assertEqual(report.status_for(1), "withheld")
        self.assertIn("MATERIAL_NOT_FOUND", report.reason_codes_for(1))
        remediation = next(r for r in report.remediations if r.code == "MATERIAL_NOT_FOUND")
        self.assertEqual(remediation.repair_from.record_id, "inner-missing")
        self.assertEqual(remediation.repair_from.version, "3")
        chain_ids = [(node.record_id, node.paragraph) for node in remediation.chain]
        self.assertIn(("outer-present", 1), chain_ids)

    def test_citation_cycle_is_detected_and_gives_cycle_entry(self):
        a_ref = RecordRef("a", "1")
        b_ref = RecordRef("b", "1")
        a = VersionedRecord.create(
            "a",
            "1",
            ["A depends on B."],
            citations=[Citation("a-to-b", Range(1, 1), b_ref, Range(1, 1))],
        )
        b = VersionedRecord.create(
            "b",
            "1",
            ["B depends on A."],
            citations=[Citation("b-to-a", Range(1, 1), a_ref, Range(1, 1))],
        )
        submission = VersionedRecord.create(
            "statement-cycle",
            "1",
            ["Cycle root."],
            citations=[Citation("root-to-a", Range(1, 1), a_ref, Range(1, 1))],
        )
        registry = MaterialRegistry([a, b, submission])
        registry.approve(a_ref, [1])
        registry.approve(b_ref, [1])
        report = ReviewEngine(registry).review_current(submission)

        self.assertEqual(report.status_for(1), "withheld")
        self.assertIn("CITATION_CYCLE", report.reason_codes_for(1))
        remediation = next(r for r in report.remediations if r.code == "CITATION_CYCLE")
        self.assertEqual(remediation.repair_from.record_id, "a")
        self.assertEqual(remediation.repair_from.paragraph, 1)
        self.assertGreaterEqual(len(remediation.chain), 4)
        self.assertEqual(remediation.chain[-1], remediation.chain[1])


if __name__ == "__main__":
    unittest.main()
