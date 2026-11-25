from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from decimal_kernel import ErrorCode, SourceColumn, decode_packed_mantissa
from decimal_kernel.errors import RowConversionException


class PackedCodecTests(unittest.TestCase):
    def test_odd_and_even_precision_layouts(self) -> None:
        odd = SourceColumn(precision=3, scale=1)
        self.assertEqual(odd.packed_width, 2)
        self.assertEqual(decode_packed_mantissa(0, bytes.fromhex("123c"), odd), 123)

        even = SourceColumn(precision=4, scale=2)
        self.assertEqual(even.packed_width, 3)
        self.assertEqual(
            decode_packed_mantissa(0, bytes.fromhex("01234c"), even),
            1234,
        )

    def test_negative_sign_and_negative_zero_normalization(self) -> None:
        spec = SourceColumn(precision=3, scale=2)
        self.assertEqual(
            decode_packed_mantissa(0, bytes.fromhex("123d"), spec),
            -123,
        )
        self.assertEqual(
            decode_packed_mantissa(3, bytes.fromhex("000d"), spec),
            0,
        )

    def test_unsigned_f_sign_is_opt_in(self) -> None:
        spec = SourceColumn(precision=3, scale=0)
        payload = bytes.fromhex("123f")
        with self.assertRaises(RowConversionException) as ctx:
            decode_packed_mantissa(0, payload, spec)
        self.assertEqual(ctx.exception.error.code, ErrorCode.INVALID_SIGN_NIBBLE)

        self.assertEqual(
            decode_packed_mantissa(
                0, payload, spec, accept_unsigned_sign=True
            ),
            123,
        )

    def test_invalid_digit_nibble_has_location(self) -> None:
        spec = SourceColumn(precision=3, scale=0)
        with self.assertRaises(RowConversionException) as ctx:
            decode_packed_mantissa(7, bytes.fromhex("a23c"), spec)
        error = ctx.exception.error
        self.assertEqual(error.row, 7)
        self.assertEqual(error.code, ErrorCode.INVALID_DIGIT_NIBBLE)
        self.assertEqual(error.byte_offset, 0)
        self.assertEqual(error.nibble_index, 0)
        self.assertEqual(error.bad_nibble, 0xA)

    def test_invalid_sign_nibble_has_location(self) -> None:
        spec = SourceColumn(precision=3, scale=0)
        with self.assertRaises(RowConversionException) as ctx:
            decode_packed_mantissa(8, bytes.fromhex("123e"), spec)
        error = ctx.exception.error
        self.assertEqual(error.row, 8)
        self.assertEqual(error.code, ErrorCode.INVALID_SIGN_NIBBLE)
        self.assertEqual(error.byte_offset, 1)
        self.assertEqual(error.nibble_index, 3)
        self.assertEqual(error.bad_nibble, 0xE)

    def test_payload_length_is_distinguished(self) -> None:
        spec = SourceColumn(precision=4, scale=2)
        with self.assertRaises(RowConversionException) as ctx:
            decode_packed_mantissa(2, b"\x12\x3c", spec)
        self.assertEqual(
            ctx.exception.error.code,
            ErrorCode.INVALID_PAYLOAD_LENGTH,
        )
        self.assertEqual(ctx.exception.error.limit, 3)

    def test_nonzero_even_precision_padding_is_source_overflow(self) -> None:
        spec = SourceColumn(precision=4, scale=2)
        with self.assertRaises(RowConversionException) as ctx:
            # First nibble is the even-precision padding nibble.
            decode_packed_mantissa(4, bytes.fromhex("91234c"), spec)
        error = ctx.exception.error
        self.assertEqual(error.code, ErrorCode.PRECISION_OVERFLOW)
        self.assertEqual(error.stage, "source_leading_padding")
        self.assertEqual(error.nibble_index, 0)


if __name__ == "__main__":
    unittest.main()
