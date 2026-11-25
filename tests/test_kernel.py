from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from decimal_kernel import (
    BatchConversionError,
    BitmapError,
    ConfigurationError,
    ErrorCode,
    convert_packed_batch,
    convert_packed_page,
)


class IndexOnlyPayloadSequence:
    def __init__(self, rows: list[object], null_rows: set[int]):
        self.rows = rows
        self.null_rows = null_rows
        self.accessed: list[int] = []

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, row: int) -> object:
        self.accessed.append(row)
        if row in self.null_rows:
            raise AssertionError(f"NULL payload row {row} was accessed")
        return self.rows[row]


class BatchKernelTests(unittest.TestCase):
    def test_empty_batch_is_accepted_by_strict_mode(self) -> None:
        result = convert_packed_batch(
            [],
            precision=1,
            source_scale=0,
            target_scale=0,
            on_error="strict",
        )
        self.assertEqual(len(result), 0)
        self.assertEqual(result.values, ())
        self.assertEqual(result.slots, ())
        self.assertEqual(result.errors, ())
        self.assertEqual(result.contiguous_target_bytes(), b"")

    def test_interleaved_nulls_never_access_payload_in_lenient(self) -> None:
        good = bytes.fromhex("123c")  # DECIMAL(3,0) +123
        sequence = IndexOnlyPayloadSequence(
            [object(), good, object(), good, object(), good],
            null_rows={0, 2, 4},
        )
        result = convert_packed_batch(
            sequence,
            precision=3,
            source_scale=0,
            target_scale=0,
            target_width=2,
            null_bitmap=[True, False, True, False, True, False],
            on_error="lenient",
        )
        self.assertEqual(result.values, (None, 123, None, 123, None, 123))
        self.assertEqual(sequence.accessed, [1, 3, 5])
        self.assertEqual(result.errors, ())

    def test_interleaved_nulls_never_access_payload_in_strict_success(self) -> None:
        good = bytes.fromhex("123c")
        sequence = IndexOnlyPayloadSequence(
            [object(), good, object(), good],
            null_rows={0, 2},
        )
        result = convert_packed_batch(
            sequence,
            precision=3,
            source_scale=0,
            target_scale=0,
            target_width=2,
            null_bitmap=bytes.fromhex("a0"),  # MSB: 1010 rows 0,2
            bitmap_bit_order="msb",
            on_error="strict",
        )
        self.assertEqual(result.values, (None, 123, None, 123))
        self.assertEqual(sequence.accessed, [1, 3])

    def test_target_endianness(self) -> None:
        payloads = [bytes.fromhex("001c")]  # DECIMAL(3,0) +1
        big = convert_packed_batch(
            payloads,
            precision=3,
            source_scale=0,
            target_scale=0,
            target_width=4,
            byte_order="big",
            on_error="lenient",
        )
        little = convert_packed_batch(
            payloads,
            precision=3,
            source_scale=0,
            target_scale=0,
            target_width=4,
            byte_order="little",
            on_error="lenient",
        )
        self.assertEqual(big.slots[0], b"\x00\x00\x00\x01")
        self.assertEqual(little.slots[0], b"\x01\x00\x00\x00")

    def test_scale_expansion_appends_exact_zeros(self) -> None:
        result = convert_packed_batch(
            [bytes.fromhex("01234c")],  # DECIMAL(4,2) +12.34 mantissa 1234
            precision=4,
            source_scale=2,
            target_scale=5,
            target_width=4,
            on_error="lenient",
        )
        self.assertEqual(result.values[0], 1_234_000)
        self.assertEqual(
            result.slots[0],
            (1_234_000).to_bytes(4, "big", signed=True),
        )

    def test_fixed_width_overflow_is_distinguished(self) -> None:
        result = convert_packed_batch(
            [bytes.fromhex("128c")],  # +128, precision 3
            precision=3,
            source_scale=0,
            target_scale=0,
            target_width=1,
            target_precision=3,
            on_error="lenient",
        )
        self.assertIsNone(result.values[0])
        self.assertEqual(len(result.errors), 1)
        error = result.errors[0]
        self.assertEqual(error.code, ErrorCode.FIXED_WIDTH_OVERFLOW)
        self.assertEqual(error.rounded_value, 128)
        self.assertEqual(result.null_bitmap.flags, (True,))

        with self.assertRaises(BatchConversionError) as ctx:
            convert_packed_batch(
                [bytes.fromhex("128c")],
                precision=3,
                source_scale=0,
                target_scale=0,
                target_width=1,
                on_error="strict",
            )
        self.assertEqual(
            ctx.exception.first_error.code,
            ErrorCode.FIXED_WIDTH_OVERFLOW,
        )

    def test_bitmap_length_mismatch_is_configuration_error(self) -> None:
        with self.assertRaises(BitmapError):
            convert_packed_batch(
                [b"\x12\x3c"] * 2,
                precision=3,
                source_scale=0,
                target_scale=0,
                null_bitmap=[True],  # one flag for two rows
            )

        with self.assertRaises(BitmapError):
            convert_packed_batch(
                [b"\x12\x3c"] * 9,
                precision=3,
                source_scale=0,
                target_scale=0,
                null_bitmap=b"\xff",  # needs two bytes for nine rows
            )

    def test_invalid_error_mode_is_configuration_error(self) -> None:
        with self.assertRaises(ConfigurationError):
            convert_packed_batch(
                [],
                precision=1,
                source_scale=0,
                target_scale=0,
                on_error="explode",
            )

    def test_unreadable_nonnull_slot_becomes_located_row_error(self) -> None:
        class BrokenSlots:
            def __len__(self) -> int:
                return 2

            def __getitem__(self, row: int) -> bytes:
                if row == 1:
                    raise RuntimeError("page slot unavailable")
                return bytes.fromhex("123c")

        result = convert_packed_batch(
            BrokenSlots(),
            precision=3,
            source_scale=0,
            target_scale=0,
            target_width=2,
            on_error="lenient",
        )
        self.assertEqual(result.values, (123, None))
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0].row, 1)
        self.assertEqual(result.errors[0].code, ErrorCode.INVALID_PAYLOAD)

    def test_contiguous_page_ignores_garbage_in_null_slots(self) -> None:
        # Four rows, packed width 2 for DECIMAL(3,0).
        page = b"".join(
            [
                b"\xee\xee",  # row 0 NULL: deliberately impossible BCD/sign
                bytes.fromhex("123c"),
                b"\xff\xff",  # row 2 NULL: deliberately impossible BCD/sign
                bytes.fromhex("000d"),  # negative zero
            ]
        )
        result = convert_packed_page(
            page,
            row_count=4,
            precision=3,
            source_scale=0,
            target_scale=0,
            target_width=2,
            null_bitmap=b"\xa0",  # MSB bits rows 0 and 2
            bitmap_bit_order="msb",
            on_error="strict",
        )
        self.assertEqual(result.values, (None, 123, None, 0))
        self.assertEqual(result.slots[3], b"\x00\x00")
        self.assertEqual(result.null_bitmap.to_bytes(), b"\xa0")

    def test_signed_width_boundaries(self) -> None:
        result = convert_packed_batch(
            [
                bytes.fromhex("127c"),  # +127
                bytes.fromhex("128d"),  # -128
                bytes.fromhex("128c"),  # +128
                bytes.fromhex("129d"),  # -129
            ],
            precision=3,
            source_scale=0,
            target_scale=0,
            target_width=1,
            target_precision=3,
            on_error="lenient",
        )
        self.assertEqual(result.values[:2], (127, -128))
        self.assertEqual(result.slots[:2], (b"\x7f", b"\x80"))
        self.assertIsNone(result.values[2])
        self.assertIsNone(result.values[3])
        self.assertEqual(
            [error.code for error in result.errors],
            [
                ErrorCode.FIXED_WIDTH_OVERFLOW,
                ErrorCode.FIXED_WIDTH_OVERFLOW,
            ],
        )
        self.assertEqual([error.row for error in result.errors], [2, 3])

    def test_rounding_tie_can_carry_and_overflow_target_precision(self) -> None:
        result = convert_packed_batch(
            [
                bytes.fromhex("09949c"),  # 99.49 -> 99
                bytes.fromhex("09950c"),  # 99.50 tie, 99 odd -> 100
                bytes.fromhex("09950d"),  # -99.50 -> -100
            ],
            precision=4,
            source_scale=2,
            target_scale=0,
            target_width=2,
            target_precision=2,
            on_error="lenient",
        )
        self.assertEqual(result.values[0], 99)
        self.assertEqual(result.slots[0], b"\x00\x63")
        self.assertEqual([error.row for error in result.errors], [1, 2])
        self.assertTrue(
            all(
                error.code is ErrorCode.PRECISION_OVERFLOW
                and error.rounded_value in (100, -100)
                and error.stage == "after_rounding"
                for error in result.errors
            )
        )

    def test_large_integer_is_not_lost_through_float(self) -> None:
        mantissa = 9_007_199_254_740_993  # 2**53 + 1
        digits = f"{mantissa:016d}"
        self.assertEqual(len(digits), 16)
        nibbles = [0] + [int(char) for char in digits] + [0xC]
        payload = bytes(
            (nibbles[index] << 4) | nibbles[index + 1]
            for index in range(0, len(nibbles), 2)
        )
        result = convert_packed_batch(
            [payload],
            precision=16,
            source_scale=0,
            target_scale=0,
            target_width=8,
            on_error="lenient",
        )
        self.assertEqual(result.values[0], mantissa)
        self.assertEqual(
            result.slots[0],
            mantissa.to_bytes(8, "big", signed=True),
        )


if __name__ == "__main__":
    unittest.main()
