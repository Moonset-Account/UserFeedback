import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import imgutil  # noqa: E402
from fwlayout.parser import parse_image  # noqa: E402


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.board = imgutil.make_board()

    def test_records_parsed_with_offsets(self):
        image = imgutil.build_image(self.board, [
            {"type": 1, "start": 256, "length": 256, "perms": 1},
            {"type": 2, "start": 512, "length": 128, "perms": 3},
        ])
        result = parse_image(image, self.board)
        self.assertTrue(result.determinable)
        self.assertEqual(result.record_count, 2)
        self.assertEqual(result.header_size, 40)
        self.assertEqual(len(result.records), 2)
        first, second = result.records
        self.assertEqual(first.offset, 8)
        self.assertFalse(first.truncated)
        self.assertEqual(first.fields,
                         {"type": 1, "start": 256, "length": 256, "perms": 1})
        self.assertEqual(second.offset, 24)
        self.assertEqual(second.fields["start"], 512)
        self.assertEqual(second.fields["perms"], 3)
        self.assertEqual(result.missing_records, 0)

    def test_magic_mismatch_stops_image(self):
        image = imgutil.build_image(self.board, [], magic=b"XXXX")
        result = parse_image(image, self.board)
        self.assertFalse(result.determinable)
        self.assertIn("magic", result.reason)

    def test_image_shorter_than_magic(self):
        result = parse_image(b"FW", self.board)
        self.assertFalse(result.determinable)

    def test_count_field_outside_image(self):
        result = parse_image(b"FWLG", self.board)
        self.assertFalse(result.determinable)
        self.assertIn("record-count", result.reason)

    def test_truncated_record(self):
        # 3 declared records but only 2 full records + 6 bytes fit.
        image = imgutil.build_image(self.board, [
            {"type": 1, "start": 256, "length": 256, "perms": 1},
            {"type": 2, "start": 512, "length": 128, "perms": 3},
            {"type": 3, "start": 1024, "length": 64, "perms": 1},
        ], image_size=8 + 2 * 16 + 6)
        result = parse_image(image, self.board)
        self.assertTrue(result.determinable)
        self.assertEqual(len(result.records), 3)
        self.assertFalse(result.records[0].truncated)
        self.assertFalse(result.records[1].truncated)
        third = result.records[2]
        self.assertTrue(third.truncated)
        self.assertEqual(third.offset, 40)
        self.assertEqual(third.available, 6)
        self.assertEqual(third.fields["type"], 3)  # 4 bytes fully present
        self.assertIsNone(third.fields["start"])   # only 2 of 4 bytes left
        self.assertIsNone(third.fields["length"])
        self.assertIsNone(third.fields["perms"])

    def test_missing_records_counted(self):
        # Declared count 5, image holds only one record slot.
        image = imgutil.build_image(self.board, [], count=5, image_size=8 + 16)
        result = parse_image(image, self.board)
        self.assertTrue(result.determinable)
        self.assertEqual(result.record_count, 5)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.missing_records, 4)


if __name__ == "__main__":
    unittest.main()
