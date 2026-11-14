import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import imgutil  # noqa: E402
from fwlayout.checks import check_image  # noqa: E402
from fwlayout.parser import parse_image  # noqa: E402


def findings_for(board, records, image_size):
    header = parse_image(imgutil.build_image(board, records,
                                             image_size=image_size), board)
    assert header.determinable
    return check_image(header, image_size, board)


def of_type(findings, ftype):
    return [f for f in findings if f["type"] == ftype]


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.board = imgutil.make_board()

    def test_adjacent_partitions_not_overlap(self):
        # P0 ends exactly where P1 starts: adjacent, not overlapping.
        findings = findings_for(self.board, [
            {"start": 256, "length": 256},
            {"start": 512, "length": 256},
        ], 4096)
        self.assertEqual(of_type(findings, "overlap"), [])
        self.assertEqual(findings, [])

    def test_true_overlap_reported_with_record_offsets(self):
        findings = findings_for(self.board, [
            {"start": 512, "length": 256},
            {"start": 700, "length": 200},
        ], 4096)
        overlaps = of_type(findings, "overlap")
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(overlaps[0]["record_offsets"], [8, 24])
        self.assertEqual(overlaps[0]["overlap_range"], [700, 768])
        self.assertEqual(overlaps[0]["partitions"][0]["range"], [512, 768])
        self.assertEqual(overlaps[0]["partitions"][1]["range"], [700, 900])

    def test_out_of_image(self):
        findings = findings_for(self.board, [
            {"start": 4000, "length": 512},
        ], 4096)
        out = of_type(findings, "out_of_image")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["record_offset"], 8)
        self.assertEqual(out[0]["range"], [4000, 4512])

    def test_partition_ending_exactly_at_image_end_ok(self):
        findings = findings_for(self.board, [
            {"start": 3840, "length": 256},
        ], 4096)
        self.assertEqual(findings, [])

    def test_reserved_region_coverage(self):
        findings = findings_for(self.board, [
            {"start": 128, "length": 64},
        ], 4096)
        reserved = of_type(findings, "reserved_region")
        self.assertEqual(len(reserved), 1)
        self.assertEqual(reserved[0]["region"], "bootrom")
        self.assertEqual(reserved[0]["record_offset"], 8)

    def test_partition_adjacent_to_reserved_region_ok(self):
        # bootrom is [0, 256); a partition starting at 256 is adjacent.
        findings = findings_for(self.board, [
            {"start": 256, "length": 64},
        ], 4096)
        self.assertEqual(findings, [])

    def test_partition_ending_at_reserved_start_ok(self):
        board = imgutil.make_board(
            reserved_regions=[{"name": "cal", "start": 1024, "length": 256}])
        findings = findings_for(board, [
            {"start": 768, "length": 256},  # ends exactly at 1024
        ], 4096)
        self.assertEqual(findings, [])

    def test_partition_overlapping_reserved_region_flagged(self):
        board = imgutil.make_board(
            reserved_regions=[{"name": "cal", "start": 1024, "length": 256}])
        findings = findings_for(board, [
            {"start": 1020, "length": 16},  # [1020, 1036) crosses 1024
        ], 4096)
        reserved = of_type(findings, "reserved_region")
        self.assertEqual(len(reserved), 1)
        self.assertEqual(reserved[0]["region"], "cal")

    def test_truncated_record_finding(self):
        image = imgutil.build_image(self.board, [
            {"start": 256, "length": 256},
            {"start": 512, "length": 128},
            {"start": 1024, "length": 64},
        ], image_size=8 + 2 * 16 + 6)
        header = parse_image(image, self.board)
        findings = check_image(header, len(image), self.board)
        trunc = of_type(findings, "truncated_record")
        self.assertEqual(len(trunc), 1)
        self.assertEqual(trunc[0]["record_offset"], 40)
        self.assertEqual(trunc[0]["record_index"], 2)


if __name__ == "__main__":
    unittest.main()
