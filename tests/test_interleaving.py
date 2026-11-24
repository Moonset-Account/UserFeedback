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


class InterleavingAndMappingTests(unittest.TestCase):
    def test_overlapping_citations_require_every_source_to_pass(self):
        a_ref = RecordRef("source-a", "1")
        b_ref = RecordRef("source-b", "1")
        source_a = VersionedRecord.create(
            "source-a", "1", ["A1", "A2", "A3"]
        )
        source_b = VersionedRecord.create(
            "source-b", "1", ["B1", "B2-secret", "B3"]
        )
        submission = VersionedRecord.create(
            "statement-overlap",
            "1",
            ["S1", "S2", "S3", "S4", "S5-original"],
            citations=[
                Citation("cite-a", Range(1, 3), a_ref, Range(1, 3)),
                Citation("cite-b", Range(2, 4), b_ref, Range(1, 3)),
            ],
        )
        registry = MaterialRegistry([source_a, source_b, submission])
        registry.approve(a_ref, [Range(1, 3)])
        registry.approve(b_ref, [1, 3])
        report = ReviewEngine(registry).review_current(submission)

        statuses = [report.status_for(i) for i in range(1, 6)]
        self.assertEqual(
            statuses,
            ["disclosable", "disclosable", "withheld", "disclosable", "disclosable"],
        )
        self.assertEqual(report.disclosable_ranges, [[1, 2], [4, 5]])
        self.assertEqual(report.withheld_ranges[0]["range"], [3, 3])

    def test_unequal_range_without_map_uses_conservative_all_dependency(self):
        source_ref = RecordRef("wide-source", "1")
        source = VersionedRecord.create(
            "wide-source", "1", ["one", "secret", "three"]
        )
        submission = VersionedRecord.create(
            "statement-wide",
            "1",
            ["whole summary depends on all three source paragraphs"],
            citations=[
                Citation("wide", Range(1, 1), source_ref, Range(1, 3))
            ],
        )
        registry = MaterialRegistry([source, submission])
        registry.approve(source_ref, [1, 3])
        report = ReviewEngine(registry).review_current(submission)
        self.assertEqual(report.status_for(1), "withheld")
        self.assertIn("NOT_IN_APPROVED_SCOPE", report.reason_codes_for(1))

    def test_explicit_paragraph_map_skips_unrelated_source_paragraph(self):
        source_ref = RecordRef("mapped-source", "1")
        source = VersionedRecord.create(
            "mapped-source", "1", ["one", "unrelated-secret", "three"]
        )
        citation = Citation(
            "map-skip",
            Range(1, 1),
            source_ref,
            Range(1, 3),
            paragraph_map={1: (1, 3)},
        )
        submission = VersionedRecord.create(
            "statement-map",
            "1",
            ["combined approved endpoints"],
            citations=[citation],
        )
        registry = MaterialRegistry([source, submission])
        registry.approve(source_ref, [1, 3])
        report = ReviewEngine(registry).review_current(submission)
        self.assertEqual(report.status_for(1), "disclosable")
        nodes = report.paragraph_results[0].source_nodes()
        source_paragraphs = {node.paragraph for node in nodes if node.record_id == "mapped-source"}
        self.assertEqual(source_paragraphs, {1, 3})

    def test_invalid_paragraph_map_is_reported(self):
        source_ref = RecordRef("bad-map-source", "1")
        source = VersionedRecord.create("bad-map-source", "1", ["one", "two"])
        citation = Citation(
            "bad-map",
            Range(1, 1),
            source_ref,
            Range(1, 2),
            paragraph_map={1: (3,)},
        )
        submission = VersionedRecord.create(
            "statement-bad-map", "1", ["text"], citations=[citation]
        )
        errors = citation.validate_map()
        self.assertTrue(errors)
        registry = MaterialRegistry([source, submission])
        report = ReviewEngine(registry).review_current(submission)
        self.assertIn("INVALID_CITATION_MAP", report.reason_codes_for(1))


if __name__ == "__main__":
    unittest.main()
