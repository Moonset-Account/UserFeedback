import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from disclosure_review import (
    Approval,
    Citation,
    MaterialRegistry,
    Range,
    RecordRef,
    ReviewEngine,
    VersionedRecord,
    fingerprint_text,
)


class BasicAdjudicationTests(unittest.TestCase):
    def setUp(self):
        self.interview_ref = RecordRef("interview-a", "1")
        self.interview = VersionedRecord(
            ref=self.interview_ref,
            paragraphs=[
                "Interview paragraph one: approved.",
                "Interview paragraph two: approved.",
                "Interview paragraph three: not approved.",
                "Interview paragraph four: approved again.",
            ],
        )
        self.submission = VersionedRecord.create(
            "statement",
            "1",
            [
                "Investigator introduction.",
                "Excerpt mapped to interview p1.",
                "Excerpt mapped to interview p2.",
                "Excerpt mapped to interview p3.",
                "Investigator closing.",
            ],
            citations=[
                Citation(
                    "cite-a",
                    Range(2, 4),
                    self.interview_ref,
                    Range(1, 3),
                )
            ],
        )
        self.registry = MaterialRegistry([self.interview, self.submission])
        self.registry.approve(self.interview_ref, [Range(1, 2), Range(4, 4)], approval_id="ap-1")
        self.engine = ReviewEngine(self.registry)

    def test_approved_and_unapproved_paragraphs_are_split(self):
        report = self.engine.review_current(self.submission)

        self.assertEqual(report.status_for(1), "disclosable")
        self.assertEqual(report.status_for(2), "disclosable")
        self.assertEqual(report.status_for(3), "disclosable")
        self.assertEqual(report.status_for(4), "withheld")
        self.assertEqual(report.status_for(5), "disclosable")

        self.assertEqual(report.disclosable_ranges, [[1, 3], [5, 5]])
        self.assertEqual(len(report.withheld_segments), 1)
        self.assertEqual(report.withheld_segments[0].paragraph_range, Range(4, 4))
        self.assertIn("NOT_IN_APPROVED_SCOPE", report.reason_codes_for(4))

        serialized = report.to_dict()
        self.assertEqual(serialized["summary"]["withheld_paragraphs"], [4])
        self.assertIn("Excerpt mapped to interview p3.", report.withheld_segments[0].text)

    def test_explicit_withheld_scope_wins(self):
        self.registry.approvals.clear()
        self.registry._approvals_by_ref.clear()
        self.registry.approve(
            self.interview_ref,
            [Range(1, 2), Range(4, 4)],
            withheld_ranges=[Range(2, 2)],
            approval_id="ap-2",
        )
        report = ReviewEngine(self.registry).review_current(self.submission)
        self.assertEqual(report.status_for(3), "withheld")
        self.assertIn("EXPLICITLY_WITHHELD", report.reason_codes_for(3))
        # p3 submission maps to interview paragraph 2 here because the range is
        # still submission 2-4 -> interview 1-3.
        self.assertEqual(report.status_for(3), "withheld")

    def test_content_hash_mismatch_defeats_range_approval(self):
        stale_approval = Approval(
            ref=self.interview_ref,
            approved_ranges=(Range(1, 1),),
            reviewed_hashes={1: fingerprint_text("wording that was actually reviewed")},
            approval_id="stale",
        )
        self.registry.add_approval(stale_approval)
        report = ReviewEngine(self.registry).review_current(self.submission)
        self.assertEqual(report.status_for(2), "withheld")
        self.assertIn("APPROVAL_CONTENT_MISMATCH", report.reason_codes_for(2))

    def test_verbatim_citation_detects_rewritten_excerpt(self):
        source_ref = RecordRef("verbatim-source", "1")
        source = VersionedRecord.create(
            "verbatim-source", "1", ["The exact approved source sentence."]
        )
        submission = VersionedRecord.create(
            "statement-v",
            "1",
            ["The exact approved source sentence, but altered."],
            citations=[
                Citation(
                    "v1",
                    Range(1, 1),
                    source_ref,
                    Range(1, 1),
                    verbatim=True,
                )
            ],
        )
        registry = MaterialRegistry([source, submission])
        registry.approve(source_ref, [1])
        report = ReviewEngine(registry).review_current(submission)
        self.assertEqual(report.status_for(1), "withheld")
        self.assertIn("VERBATIM_EXCERPT_MISMATCH", report.reason_codes_for(1))


if __name__ == "__main__":
    unittest.main()
