"""Chunk-boundary and end-of-stream property tests.

The importer must produce identical contacts, error categories and
logical record numbers no matter how the input is split into feed()
chunks, and must handle stream termination deterministically.
"""

import random
import unittest

import helpers
import transforms


class ChunkBoundaryTests(unittest.TestCase):
    def test_fixed_size_chunks(self):
        for sample_id in helpers.ALL_SAMPLES:
            text = helpers.sample_text(sample_id)
            baseline = helpers.parse_text(text)
            for size in (1, 2, 3, 5, 8, 64):
                chunks = helpers.split_into_sizes(text, [size] * (len(text) // size))
                case_id = "chunk-fixed/%s/%d" % (sample_id, size)
                with self.subTest(case_id=case_id):
                    actual = helpers.parse_text(text, chunks)
                    helpers.assert_signature_equal(
                        case_id,
                        sample_id,
                        "fixed chunk size %d" % size,
                        text,
                        baseline,
                        actual,
                    )

    def test_random_chunk_boundaries(self):
        rng = random.Random(helpers.SEED)
        for sample_id in helpers.ALL_SAMPLES:
            text = helpers.sample_text(sample_id)
            baseline = helpers.parse_text(text)
            for trial in range(5):
                if len(text) > 1:
                    n_splits = rng.randint(1, min(8, len(text) - 1))
                    points = sorted(rng.sample(range(1, len(text)), n_splits))
                else:
                    points = []
                chunks, prev = [], 0
                for p in points:
                    chunks.append(text[prev:p])
                    prev = p
                chunks.append(text[prev:])
                case_id = "chunk-random/%s/%d" % (sample_id, trial)
                with self.subTest(case_id=case_id):
                    actual = helpers.parse_text(text, chunks)
                    helpers.assert_signature_equal(
                        case_id,
                        sample_id,
                        "random split points %s (seed %d)" % (points, helpers.SEED),
                        text,
                        baseline,
                        actual,
                    )

    def test_crlf_split_between_cr_and_lf(self):
        # Splitting exactly between CR and LF of a CRLF separator must not
        # create a phantom record.
        text = transforms.transform_newlines(helpers.sample_text("valid_quoted"), "\r\n")
        baseline = helpers.parse_text(text)
        splits = [i for i, ch in enumerate(text) if ch == "\r"]
        self.assertTrue(splits, "expected CRLF separators in transformed sample")
        for i in splits:
            chunks = [text[: i + 1], text[i + 1:]]
            case_id = "chunk-crlf-split/%d" % i
            with self.subTest(case_id=case_id):
                actual = helpers.parse_text(text, chunks)
                helpers.assert_signature_equal(
                    case_id,
                    "valid_quoted",
                    "chunk boundary between CR and LF at offset %d" % i,
                    text,
                    baseline,
                    actual,
                )

    def test_empty_feeds_are_noops(self):
        text = helpers.sample_text("valid_basic")
        baseline = helpers.parse_text(text)
        chunks = ["", text[:5], "", "", text[5:], ""]
        actual = helpers.parse_text(text, chunks)
        helpers.assert_signature_equal(
            "chunk-empty-feeds", "valid_basic",
            "interleaved empty feed('') calls", text, baseline, actual,
        )


class EndOfStreamTests(unittest.TestCase):
    def test_trailing_newline_optional(self):
        with_nl = helpers.sample_text("valid_basic")
        self.assertTrue(with_nl.endswith("\n"))
        without_nl = with_nl[:-1]
        base = helpers.parse_text(with_nl)
        actual = helpers.parse_text(without_nl)
        helpers.assert_signature_equal(
            "eof/no-trailing-newline", "valid_basic",
            "final record without trailing newline", without_nl, base, actual,
        )

    def test_close_inside_quoted_field_reports_unterminated(self):
        text = helpers.sample_text("invalid_unterminated")
        result = helpers.parse_text(text)
        self.assertEqual(
            result["errors"],
            [{"record": 3, "category": "ROW_UNTERMINATED_QUOTE"}],
        )
        self.assertEqual(len(result["contacts"]), 1)

    def test_close_after_closing_quote_flushes_record(self):
        text = 'name,email\n"Eve",eve@example.com'
        result = helpers.parse_text(text)
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["contacts"]), 1)
        self.assertEqual(result["contacts"][0]["name"], "Eve")

    def test_close_mid_quote_across_chunks(self):
        # Unterminated quote detected at close() must carry the logical
        # record number regardless of where chunks were split.
        text = helpers.sample_text("invalid_unterminated")
        baseline = helpers.parse_text(text)
        for size in (1, 3, 7):
            chunks = helpers.split_into_sizes(text, [size] * (len(text) // size))
            with self.subTest(case_id="eof/unterminated-chunked/%d" % size):
                actual = helpers.parse_text(text, chunks)
                helpers.assert_signature_equal(
                    "eof/unterminated-chunked/%d" % size,
                    "invalid_unterminated",
                    "chunk size %d, unterminated quote at close" % size,
                    text,
                    baseline,
                    actual,
                )


if __name__ == "__main__":
    unittest.main()
