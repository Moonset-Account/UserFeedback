from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from decimal_kernel import (
    BatchConversionError,
    ErrorCode,
    convert_packed_batch,
)


class PayloadAccessSpy:
    """Sequence that fails if the kernel even indexes a NULL payload slot."""

    def __init__(self, slots: list[object], null_rows: set[int]):
        self._slots = slots
        self._null_rows = null_rows
        self.accessed: list[int] = []

    def __len__(self) -> int:
        return len(self._slots)

    def __getitem__(self, row: int) -> object:
        self.accessed.append(row)
        if row in self._null_rows:
            raise AssertionError(f"undefined payload at NULL row {row} was read")
        return self._slots[row]


class MidpointSampleAndErrorRowTests(unittest.TestCase):
    """The public byte-level sample requested by the task.

    Source column: DECIMAL(4,3), packed width 3.
    Target column: fixed signed 16-bit integer, scale 0, target precision 1
    (precision 1 makes the 9.999 -> 10 carry an overflow).
    Bitmap is MSB-first.
    """

    maxDiff = None

    def setUp(self) -> None:
        self.null_rows = {0, 10}
        self.source_hex = {
            1: "01 49 9C",  # +1.499, just below 1.500 midpoint
            2: "01 50 0C",  # +1.500, exact midpoint; 1 is odd -> 2
            3: "01 50 1C",  # +1.501, just above midpoint
            4: "01 49 9D",  # -1.499
            5: "01 50 0D",  # -1.500, tie -> even -2
            6: "01 50 1D",  # -1.501
            7: "01 A0 0C",  # illegal digit A at nibble 2
            8: "01 50 0E",  # illegal sign E
            9: "09 99 9C",  # +9.999 -> direct rounding gives 10
        }
        slots: list[object] = [object() for _ in range(11)]
        for row, hex_text in self.source_hex.items():
            slots[row] = bytes.fromhex(hex_text.replace(" ", ""))
        self.payloads = PayloadAccessSpy(slots, self.null_rows)
        self.bitmap = bytes.fromhex("8020")  # rows 0 and 10 in MSB order
        self.kwargs = dict(
            precision=4,
            source_scale=3,
            target_scale=0,
            target_width=2,
            target_precision=1,
            byte_order="big",
            null_bitmap=self.bitmap,
            bitmap_bit_order="msb",
        )

    def test_lenient_bytes_error_rows_and_target_bitmap(self) -> None:
        result = convert_packed_batch(
            self.payloads, on_error="lenient", **self.kwargs
        )

        expected_values = (
            None,
            1,
            2,
            2,
            -1,
            -2,
            -2,
            None,
            None,
            None,
            None,
        )
        self.assertEqual(result.values, expected_values)

        expected_hex = [
            None,
            "0001",
            "0002",
            "0002",
            "ffff",
            "fffe",
            "fffe",
            None,
            None,
            None,
            None,
        ]
        self.assertEqual(
            [slot.hex() if slot is not None else None for slot in result.slots],
            expected_hex,
        )

        self.assertEqual([error.row for error in result.errors], [7, 8, 9])
        self.assertEqual(
            [error.code for error in result.errors],
            [
                ErrorCode.INVALID_DIGIT_NIBBLE,
                ErrorCode.INVALID_SIGN_NIBBLE,
                ErrorCode.PRECISION_OVERFLOW,
            ],
        )

        digit_error, sign_error, overflow_error = result.errors
        self.assertEqual(digit_error.byte_offset, 1)
        self.assertEqual(digit_error.nibble_index, 2)
        self.assertEqual(digit_error.bad_nibble, 0xA)

        self.assertEqual(sign_error.byte_offset, 2)
        self.assertEqual(sign_error.nibble_index, 5)
        self.assertEqual(sign_error.bad_nibble, 0xE)

        self.assertEqual(overflow_error.rounded_value, 10)
        self.assertEqual(overflow_error.limit, 9)
        self.assertEqual(overflow_error.stage, "after_rounding")

        # Source NULLs plus lenient error rows 7,8,9 are target NULLs.
        self.assertEqual(result.null_bitmap.to_bytes("msb"), b"\x81\xe0")

        for null_row in self.null_rows:
            self.assertNotIn(null_row, self.payloads.accessed)

    def test_strict_rejects_entire_batch_with_same_error_rows(self) -> None:
        with self.assertRaises(BatchConversionError) as ctx:
            convert_packed_batch(
                self.payloads, on_error="strict", **self.kwargs
            )

        error = ctx.exception
        self.assertEqual(error.rows, (7, 8, 9))
        self.assertEqual(
            [item.code for item in error.errors],
            [
                ErrorCode.INVALID_DIGIT_NIBBLE,
                ErrorCode.INVALID_SIGN_NIBBLE,
                ErrorCode.PRECISION_OVERFLOW,
            ],
        )
        for null_row in self.null_rows:
            self.assertNotIn(null_row, self.payloads.accessed)

    def test_source_sample_bytes_are_exactly_packed_layout(self) -> None:
        # This test independently checks the documented hex samples rather than
        # generating them with a helper.
        self.assertEqual(
            [self.payloads[row].hex(" ").upper() for row in range(1, 10)],
            [self.source_hex[row] for row in range(1, 10)],
        )


if __name__ == "__main__":
    unittest.main()
