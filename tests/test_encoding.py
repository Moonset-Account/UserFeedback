"""LEB128 and full envelope round-trip tests."""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compressor import (
    ASSIGN,
    COND,
    SADD,
    SWAP,
    Barrier,
    Declaration,
    Operation,
    Receipt,
    Stream,
    Update,
    decode_declaration,
    decode_stream,
    encode_declaration,
)
from compressor.encoding import (
    decode_signed,
    decode_unsigned,
    encode_signed,
    encode_stream,
    encode_unsigned,
    encoded_length,
)


class Leb128Tests(unittest.TestCase):
    def test_unsigned_known_values(self):
        cases = {
            0: b"\x00",
            1: b"\x01",
            127: b"\x7f",
            128: b"\x80\x01",
            300: b"\xac\x02",
            16384: b"\x80\x80\x01",
        }
        for value, blob in cases.items():
            self.assertEqual(encode_unsigned(value), blob)
            decoded, offset = decode_unsigned(blob, 0)
            self.assertEqual(decoded, value)
            self.assertEqual(offset, len(blob))

    def test_signed_known_values(self):
        cases = {
            0: b"\x00",
            -1: b"\x7f",
            1: b"\x01",
            -2: b"\x7e",
            63: b"\x3f",
            -64: b"\x40",
            127: b"\xff\x00",
            -128: b"\x80\x7f",
        }
        for value, blob in cases.items():
            self.assertEqual(encode_signed(value), blob)
            decoded, offset = decode_signed(blob, 0)
            self.assertEqual(decoded, value)
            self.assertEqual(offset, len(blob))

    def test_random_roundtrip_signed(self):
        rng = random.Random(99)
        for _ in range(500):
            value = rng.randint(-(10 ** 18), 10 ** 18)
            decoded, offset = decode_signed(encode_signed(value), 0)
            self.assertEqual(decoded, value)

    def test_unsigned_rejects_negative(self):
        with self.assertRaises(ValueError):
            encode_unsigned(-1)


class EnvelopeTests(unittest.TestCase):
    def _declaration(self):
        return Declaration(
            registers=((-10 ** 9, 10 ** 9), (0, 7), (-3, 3)),
            constants=(-10 ** 9, -5, 0, 5, 7, 10 ** 9),
            operations=(
                Operation(ASSIGN, target=0, constant=5),
                Operation(SWAP, target=0, source=2),
                Operation(SADD, target=2, constant=-5),
                Operation(
                    COND, target=1, source=2, low=-5, high=0,
                    constant=10 ** 9, action=ASSIGN,
                ),
                Operation(
                    COND, target=2, source=1, low=0, high=7,
                    constant=-5, action=SADD,
                ),
            ),
        )

    def test_declaration_roundtrip(self):
        decl = self._declaration()
        blob = encode_declaration(decl)
        decoded, offset = decode_declaration(blob, 0)
        self.assertEqual(decoded, decl)
        self.assertEqual(offset, len(blob))

    def test_full_stream_roundtrip_with_all_message_forms(self):
        decl = self._declaration()
        stream = Stream(
            decl,
            (
                Update(0),
                Update(1, receipt=10),
                Barrier((0, 2)),
                Barrier((1,), receipt=11),
                Receipt(12),
                Update(4),
            ),
        )
        blob = encode_stream(stream)
        self.assertEqual(encoded_length(stream), len(blob))
        decoded = decode_stream(blob)
        self.assertEqual(decoded, stream)

    def test_trailing_bytes_rejected(self):
        stream = Stream(self._declaration(), (Update(0),))
        blob = encode_stream(stream) + b"\x00"
        with self.assertRaises(ValueError):
            decode_stream(blob)

    def test_unknown_tag_rejected(self):
        stream = Stream(self._declaration(), (Update(0),))
        blob = bytearray(encode_stream(stream))
        blob[-2] = 99  # message tag sits just before the operation index
        with self.assertRaises(ValueError):
            decode_stream(bytes(blob))


if __name__ == "__main__":
    unittest.main()
