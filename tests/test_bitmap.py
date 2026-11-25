from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from decimal_kernel import BitmapError, NullBitmap


class NullBitmapTests(unittest.TestCase):
    def test_msb_raw_byte_mapping(self) -> None:
        bitmap = NullBitmap.from_bytes(b"\x81", 8, "msb")
        self.assertEqual(
            bitmap.flags,
            (True, False, False, False, False, False, False, True),
        )
        self.assertEqual(bitmap.to_bytes(), b"\x81")

    def test_lsb_raw_byte_mapping(self) -> None:
        bitmap = NullBitmap.from_bytes(b"\x81", 8, "lsb")
        self.assertEqual(
            bitmap.flags,
            (True, False, False, False, False, False, False, True),
        )
        # Both LSB bits 0 and 7 produce the same byte 0x81 in this case.
        self.assertEqual(bitmap.to_bytes("lsb"), b"\x81")

    def test_interleaved_bits_cross_byte_boundary_msb(self) -> None:
        flags = [i % 2 == 0 for i in range(11)]
        bitmap = NullBitmap.from_bools(flags, "msb")
        raw = bitmap.to_bytes()
        # Rows 0,2,4,6 -> 0xAA; row 8 -> 0x80, row 10 -> 0x20 => 0xA0.
        self.assertEqual(raw, b"\xaa\xa0")
        parsed = NullBitmap.from_bytes(raw, 11, "msb")
        self.assertEqual(parsed.flags, tuple(flags))

    def test_interleaved_bits_cross_byte_boundary_lsb(self) -> None:
        flags = [i % 2 == 1 for i in range(11)]
        bitmap = NullBitmap.from_bools(flags, "lsb")
        raw = bitmap.to_bytes("lsb")
        # Odd rows 1,3,5,7 -> 0xAA; row 9 -> bit1 of second byte = 0x02.
        self.assertEqual(raw, b"\xaa\x02")

    def test_unused_trailing_bits_are_zero_on_serialization(self) -> None:
        bitmap = NullBitmap.from_bools([True, False, True], "msb")
        self.assertEqual(bitmap.to_bytes(), b"\xa0")

    def test_truncated_bitmap_is_rejected(self) -> None:
        with self.assertRaises(BitmapError):
            NullBitmap.from_bytes(b"\xff", 9, "msb")

    def test_error_rows_become_null(self) -> None:
        bitmap = NullBitmap.from_bools([True, False, False, False], "msb")
        updated = bitmap.with_error_rows_null([2])
        self.assertEqual(updated.flags, (True, False, True, False))
        # Original is immutable.
        self.assertEqual(bitmap.flags, (True, False, False, False))


if __name__ == "__main__":
    unittest.main()
