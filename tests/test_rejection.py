"""Stable rejection-category property tests.

Illegal samples must be rejected with stable categories and logical
record numbers; the classification must survive chunking and (where the
sample is well-formed enough to round-trip) representation transforms.
"""

import random
import unittest

import helpers
import transforms
from expectations import EXPECTED_SIGNATURES


class RejectionCategoryTests(unittest.TestCase):
    def test_expected_signatures_for_all_samples(self):
        for case_id in sorted(EXPECTED_SIGNATURES):
            text = helpers.sample_text(case_id)
            with self.subTest(case_id="rejection/%s" % case_id):
                actual = helpers.parse_text(text)
                helpers.assert_signature_equal(
                    "rejection/%s" % case_id,
                    case_id,
                    "none (canonical sample)",
                    text,
                    EXPECTED_SIGNATURES[case_id],
                    actual,
                )

    def test_logical_record_numbers_ignore_embedded_newlines(self):
        # A quoted field spanning physical lines must not shift the record
        # numbers of later rows.
        text = helpers.sample_text("invalid_multiline_record_numbering")
        result = helpers.parse_text(text)
        self.assertEqual(
            result["errors"],
            [{"record": 3, "category": "ROW_INVALID_EMAIL"}],
            "error must point at logical record 3, not physical line 4",
        )

    def test_newline_style_preserves_rejection_categories(self):
        # Even for rejected samples, changing record-separator style must
        # not change the classification (see transforms.transform_newlines
        # for why rewriting only out-of-quote newlines is safe).
        for case_id in sorted(EXPECTED_SIGNATURES):
            text = helpers.sample_text(case_id)
            baseline = helpers.parse_text(text)
            for style_name, style in sorted(transforms.NEWLINE_STYLES.items()):
                variant = transforms.transform_newlines(text, style)
                sub_id = "rejection-newline/%s/%s" % (case_id, style_name)
                with self.subTest(case_id=sub_id):
                    actual = helpers.parse_text(variant)
                    helpers.assert_signature_equal(
                        sub_id, case_id,
                        "newline style -> %s" % style_name,
                        variant, baseline, actual,
                    )

    def test_rejection_categories_stable_under_random_chunking(self):
        rng = random.Random(helpers.SEED)
        for case_id in sorted(EXPECTED_SIGNATURES):
            text = helpers.sample_text(case_id)
            baseline = helpers.parse_text(text)
            for trial in range(3):
                if len(text) > 1:
                    n_splits = rng.randint(1, min(6, len(text) - 1))
                    points = sorted(rng.sample(range(1, len(text)), n_splits))
                else:
                    points = []
                chunks, prev = [], 0
                for p in points:
                    chunks.append(text[prev:p])
                    prev = p
                chunks.append(text[prev:])
                sub_id = "rejection-chunk/%s/%d" % (case_id, trial)
                with self.subTest(case_id=sub_id):
                    actual = helpers.parse_text(text, chunks)
                    helpers.assert_signature_equal(
                        sub_id, case_id,
                        "random split points %s (seed %d)" % (points, helpers.SEED),
                        text, baseline, actual,
                    )


if __name__ == "__main__":
    unittest.main()
