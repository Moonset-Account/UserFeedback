import sys
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memory_reservation import (  # noqa: E402
    AddressRange,
    build_free_page_ranges,
    coerce_range,
    expand_reserved_to_pages,
)


class RangeParsingTests(unittest.TestCase):
    def test_half_open_tuple_and_length_helper(self):
        self.assertEqual(coerce_range((0x1000, 0x3000)), AddressRange(0x1000, 0x3000))
        self.assertEqual(
            AddressRange.from_length(0x1000, 0x2000),
            AddressRange(0x1000, 0x3000),
        )

    def test_firmware_base_length_mapping(self):
        entry = {"base_addr": 0x100000, "length": 0x4000, "type": "reserved"}
        parsed = coerce_range(entry)
        self.assertEqual(parsed, AddressRange(0x100000, 0x104000, "reserved"))

    def test_invalid_range(self):
        with self.assertRaises(ValueError):
            AddressRange(0x3000, 0x2000)
        with self.assertRaises(ValueError):
            coerce_range((-1, 0x1000))


class ReservedPageExpansionTests(unittest.TestCase):
    PS = 0x1000

    def test_upper_half_reservation_removes_whole_page(self):
        expanded = expand_reserved_to_pages(
            [AddressRange(0x3800, 0x3C00)], self.PS
        )
        self.assertEqual(expanded, (AddressRange(0x3000, 0x4000),))

    def test_lower_half_reservation_removes_whole_page(self):
        expanded = expand_reserved_to_pages(
            [AddressRange(0x2000, 0x2800)], self.PS
        )
        self.assertEqual(expanded, (AddressRange(0x2000, 0x3000),))

    def test_cross_page_partial_reservation_removes_both_pages(self):
        expanded = expand_reserved_to_pages(
            [AddressRange(0x2800, 0x3800)], self.PS
        )
        self.assertEqual(expanded, (AddressRange(0x2000, 0x4000),))

    def test_zero_length_reservation_does_not_remove_page(self):
        expanded = expand_reserved_to_pages(
            [AddressRange(0x1800, 0x1800)], self.PS
        )
        self.assertEqual(expanded, ())

    def test_reserved_range_wins_over_available_range(self):
        free = build_free_page_ranges(
            available_ranges=[AddressRange(0, 0x20000)],
            reserved_ranges=[(0x3800, 0x3C00)],
            page_size=self.PS,
        )
        self.assertEqual(
            free,
            (AddressRange(0, 0x3000), AddressRange(0x4000, 0x20000)),
        )

    def test_unaligned_available_boundary_pages_are_not_offered(self):
        free = build_free_page_ranges(
            available_ranges=[AddressRange(0x1800, 0x5800)],
            reserved_ranges=[],
            page_size=self.PS,
        )
        self.assertEqual(free, (AddressRange(0x2000, 0x5000),))

    def test_interleaved_firmware_ranges(self):
        free = build_free_page_ranges(
            available_ranges=[
                (0x00000, 0x20000),
                (0x30000, 0x50000),
                (0x60000, 0x70000),
            ],
            reserved_ranges=[
                (0x11800, 0x12800),  # expands to 0x11000-0x13000
                {"base": 0x40000, "length": 0x800},  # expands to one page
            ],
            page_size=self.PS,
        )
        self.assertEqual(
            free,
            (
                AddressRange(0x00000, 0x11000),
                AddressRange(0x13000, 0x20000),
                AddressRange(0x30000, 0x40000),
                AddressRange(0x41000, 0x50000),
                AddressRange(0x60000, 0x70000),
            ),
        )


if __name__ == "__main__":
    unittest.main()
