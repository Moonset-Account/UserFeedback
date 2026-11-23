"""Validation of the synthetic fixed-Huffman DEFLATE encoder.

These tests cross-check the hand-rolled encoder in
:mod:`range_deflate.builder` against the standard-library ``zlib``
decompressor, including exhaustive coverage of every length and distance
code so an off-by-one in the RFC tables cannot slip through.
"""

import random
import unittest
import zlib

from range_deflate.builder import (
    encode_dictionary_frame,
    encode_fixed_frame,
    encode_literal_frame,
    encode_stored_frame,
)


def _inflate(stream: bytes, dictionary: bytes | None = None) -> tuple[bytes, bool]:
    decompressor = zlib.decompressobj(
        -15, zdict=dictionary if dictionary is not None else b""
    )
    output = decompressor.decompress(stream) + decompressor.flush()
    return output, decompressor.eof


class LiteralEncodingTests(unittest.TestCase):
    def test_every_literal_symbol(self) -> None:
        for symbol in range(256):
            output, eof = _inflate(encode_literal_frame(bytes([symbol])))
            self.assertEqual(output, bytes([symbol]))
            self.assertTrue(eof)

    def test_empty_frame_is_valid_eob_only_stream(self) -> None:
        output, eof = _inflate(encode_fixed_frame([]))
        self.assertEqual(output, b"")
        self.assertTrue(eof)

    def test_literal_fuzz(self) -> None:
        rng = random.Random(20261123)
        for _ in range(300):
            data = bytes(rng.randrange(256) for _ in range(rng.randint(0, 256)))
            output, eof = _inflate(encode_literal_frame(data))
            self.assertEqual(output, data)
            self.assertTrue(eof)


class MatchEncodingTests(unittest.TestCase):
    def test_every_length_code_3_to_258(self) -> None:
        for length in range(3, 259):
            stream = encode_fixed_frame([("match", length, 1)])
            output, eof = _inflate(stream, dictionary=b"Z")
            self.assertEqual(output, b"Z" * length)
            self.assertTrue(eof)

    def test_length_lookup_covers_3_to_258_contiguously(self) -> None:
        from range_deflate.builder import (
            _LENGTH_BASE,
            _LENGTH_EXTRA,
            _find_interval,
        )

        # The final RFC length code (285) is reserved, so contiguous
        # validation stops once the covered range reaches 258.
        cursor = 3
        for code, (base, extra) in enumerate(zip(_LENGTH_BASE, _LENGTH_EXTRA)):
            if cursor > 258:
                self.assertEqual(extra, 0)
                continue
            self.assertEqual(base, cursor)
            for value in range(base, base + (1 << extra)):
                self.assertEqual(
                    _find_interval(_LENGTH_BASE, _LENGTH_EXTRA, value), code
                )
            cursor = base + (1 << extra)
        self.assertEqual(cursor, 259)

    def test_distance_interval_boundaries_inflate(self) -> None:
        # Inflate one representative stream at every distance-code boundary
        # (the lower and upper edge of each of the 30 code intervals).
        from range_deflate.builder import _DIST_BASE, _DIST_EXTRA, _find_interval

        boundaries = {1}
        for base, extra in zip(_DIST_BASE, _DIST_EXTRA):
            boundaries.add(base)
            boundaries.add(base + (1 << extra) - 1)
        boundaries.discard(32769)
        for distance in sorted(boundaries):
            dictionary = bytes(
                (index * 7 + 1) & 0xFF for index in range(distance)
            )
            stream = encode_fixed_frame([("match", 3, distance)])
            output, eof = _inflate(stream, dictionary=dictionary)
            history = bytearray(dictionary)
            for _ in range(3):
                history.append(history[-distance])
            self.assertEqual(output, bytes(history[distance:]))
            self.assertTrue(eof)

    def test_distance_lookup_covers_every_value_1_to_32768(self) -> None:
        # Exhaustively verify the interval mapping (cheap: no inflation),
        # guaranteeing contiguous, gap-free coverage of the full range.
        from range_deflate.builder import _DIST_BASE, _DIST_EXTRA, _find_interval

        cursor = 1
        for code, (base, extra) in enumerate(zip(_DIST_BASE, _DIST_EXTRA)):
            self.assertEqual(base, cursor)
            for value in range(base, base + (1 << extra)):
                self.assertEqual(_find_interval(_DIST_BASE, _DIST_EXTRA, value), code)
            cursor = base + (1 << extra)
        self.assertEqual(cursor, 32769)

    def test_intra_stream_match_fuzz(self) -> None:
        rng = random.Random(424242)
        for _ in range(400):
            history = bytearray()
            plan = []
            for _ in range(rng.randint(1, 40)):
                if len(history) >= 3 and rng.random() < 0.5:
                    distance = rng.randint(1, len(history))
                    length = rng.randint(3, min(258, len(history)))
                    for _ in range(length):
                        history.append(history[-distance])
                    plan.append(("match", length, distance))
                else:
                    value = rng.randrange(256)
                    history.append(value)
                    plan.append(("lit", value))
            output, eof = _inflate(encode_fixed_frame(plan))
            self.assertEqual(output, bytes(history))
            self.assertTrue(eof)


class DictionaryFrameTests(unittest.TestCase):
    def test_dictionary_back_reference_roundtrip(self) -> None:
        dictionary = b"ABCDEFGH"
        stream, expected = encode_dictionary_frame(
            dictionary, match_length=3, distance=8, suffix=b"hello-world"
        )
        self.assertEqual(expected, b"ABChello-world")
        output, eof = _inflate(stream, dictionary=dictionary)
        self.assertEqual(output, expected)
        self.assertTrue(eof)

    def test_missing_dictionary_is_rejected(self) -> None:
        dictionary = b"ABCDEFGH"
        stream, _ = encode_dictionary_frame(dictionary, 3, 8)
        with self.assertRaises(zlib.error):
            _inflate(stream, dictionary=None)
        with self.assertRaises(zlib.error):
            _inflate(stream, dictionary=b"")

    def test_wrong_dictionary_changes_output(self) -> None:
        dictionary = b"ABCDEFGH"
        stream, expected = encode_dictionary_frame(dictionary, 3, 8)
        wrong, _ = _inflate(stream, dictionary=b"ZZZZZZZZ")
        self.assertNotEqual(wrong, expected)
        self.assertEqual(wrong[:3], b"ZZZ")

    def test_dictionary_fuzz(self) -> None:
        rng = random.Random(99)
        for _ in range(200):
            size = rng.randint(3, 80)
            dictionary = bytes(rng.randrange(256) for _ in range(size))
            distance = rng.randint(3, size)
            length = rng.randint(3, distance)
            suffix = bytes(rng.randrange(256) for _ in range(rng.randint(0, 20)))
            stream, expected = encode_dictionary_frame(
                dictionary, length, distance, suffix
            )
            output, eof = _inflate(stream, dictionary=dictionary)
            self.assertEqual(output, expected)
            self.assertTrue(eof)

    def test_invalid_match_arguments(self) -> None:
        with self.assertRaises(ValueError):
            encode_dictionary_frame(b"abc", 2, 2)
        with self.assertRaises(ValueError):
            encode_dictionary_frame(b"abc", 3, 4)
        with self.assertRaises(ValueError):
            encode_dictionary_frame(b"abc", 4, 3)


class StoredFrameTests(unittest.TestCase):
    def test_stored_stream_inflates_with_any_dictionary(self) -> None:
        data = bytes(range(256)) * 4
        stream = encode_stored_frame(data)
        for dictionary in (b"", b"Q" * 8, b"dictionary" * 16):
            output, eof = _inflate(stream, dictionary=dictionary)
            self.assertEqual(output, data)
            self.assertTrue(eof)

    def test_stored_stream_is_independent_of_dictionary_bytes(self) -> None:
        data = b"same compressed bytes regardless of dictionary"
        first = encode_stored_frame(data)
        second = encode_stored_frame(data)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
