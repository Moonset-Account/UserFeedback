"""Equivalence-transform property tests.

For every legal seed sample, rewriting newline style, column order or
field quoting must not change the imported contacts or the row error
classification.  A fixed random seed (helpers.SEED) drives permutation
and combination choices so failures reproduce exactly.
"""

import random
import unittest

import helpers
import transforms


class NewlineStyleTests(unittest.TestCase):
    def test_newline_style_invariance(self):
        for sample_id in helpers.ALL_SAMPLES:
            text = helpers.sample_text(sample_id)
            baseline = helpers.parse_text(text)
            for style_name, style in sorted(transforms.NEWLINE_STYLES.items()):
                variant = transforms.transform_newlines(text, style)
                with self.subTest(case_id="newline/%s/%s" % (sample_id, style_name)):
                    actual = helpers.parse_text(variant)
                    helpers.assert_signature_equal(
                        "newline/%s/%s" % (sample_id, style_name),
                        sample_id,
                        "newline style -> %s" % style_name,
                        variant,
                        baseline,
                        actual,
                    )

    def test_newline_transform_really_rewrites(self):
        # Guard against a vacuous transform: an LF sample rewritten to
        # CRLF/CR must differ byte-wise but not semantically.
        text = helpers.sample_text("valid_basic")
        self.assertIn("\n", text)
        self.assertNotEqual(transforms.transform_newlines(text, "\r\n"), text)
        self.assertNotEqual(transforms.transform_newlines(text, "\r"), text)


class ColumnOrderTests(unittest.TestCase):
    def test_column_order_invariance(self):
        rng = random.Random(helpers.SEED)
        for sample_id in helpers.ROUNDTRIP_SAMPLES:
            text = helpers.sample_text(sample_id)
            baseline = helpers.parse_text(text)
            width = len(transforms.parse_rows(text)[0])
            perms = [list(range(width))]  # identity as a control
            for _ in range(3):
                perm = list(range(width))
                rng.shuffle(perm)
                perms.append(perm)
            for perm in perms:
                variant = transforms.transform_columns(text, perm)
                case_id = "columns/%s/%s" % (sample_id, "-".join(map(str, perm)))
                with self.subTest(case_id=case_id):
                    actual = helpers.parse_text(variant)
                    helpers.assert_signature_equal(
                        case_id,
                        sample_id,
                        "column permutation %s" % (perm,),
                        variant,
                        baseline,
                        actual,
                    )


class QuotingTests(unittest.TestCase):
    def test_quoting_style_invariance(self):
        for sample_id in helpers.ROUNDTRIP_SAMPLES:
            text = helpers.sample_text(sample_id)
            baseline = helpers.parse_text(text)
            for mode in transforms.QUOTING_MODES:
                variant = transforms.transform_quoting(text, mode)
                case_id = "quoting/%s/%s" % (sample_id, mode)
                with self.subTest(case_id=case_id):
                    actual = helpers.parse_text(variant)
                    helpers.assert_signature_equal(
                        case_id,
                        sample_id,
                        "quoting mode -> %s" % mode,
                        variant,
                        baseline,
                        actual,
                    )


class CombinedTransformTests(unittest.TestCase):
    def test_combined_transforms(self):
        rng = random.Random(helpers.SEED)
        styles = sorted(transforms.NEWLINE_STYLES)
        for sample_id in helpers.ROUNDTRIP_SAMPLES:
            text = helpers.sample_text(sample_id)
            baseline = helpers.parse_text(text)
            rows = transforms.parse_rows(text)
            width = len(rows[0])
            for trial in range(6):
                perm = list(range(width))
                rng.shuffle(perm)
                style_name = rng.choice(styles)
                quoting = rng.choice(transforms.QUOTING_MODES)
                variant = transforms.serialize_rows(
                    [transforms.apply_perm(r, perm) for r in rows],
                    transforms.NEWLINE_STYLES[style_name],
                    quoting,
                )
                case_id = "combined/%s/%d" % (sample_id, trial)
                with self.subTest(case_id=case_id):
                    actual = helpers.parse_text(variant)
                    helpers.assert_signature_equal(
                        case_id,
                        sample_id,
                        "perm=%s newline=%s quoting=%s" % (perm, style_name, quoting),
                        variant,
                        baseline,
                        actual,
                    )


if __name__ == "__main__":
    unittest.main()
