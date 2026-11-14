import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import imgutil  # noqa: E402
from fwlayout.board import BoardError  # noqa: E402


class BoardParsingTests(unittest.TestCase):
    def test_valid_board(self):
        board = imgutil.make_board()
        self.assertEqual(board.record_size, 16)
        self.assertEqual(board.table_start, 8)
        self.assertEqual(board.endianness, "little")
        self.assertEqual([r.name for r in board.reserved_regions], ["bootrom"])

    def test_bad_endianness_rejected(self):
        with self.assertRaises(BoardError):
            imgutil.make_board(endianness="middle")

    def test_missing_start_field_rejected(self):
        with self.assertRaises(BoardError):
            imgutil.make_board(record={"fields": [
                {"name": "length", "size": 4},
                {"name": "perms", "size": 4},
            ]})

    def test_duplicate_field_names_rejected(self):
        with self.assertRaises(BoardError):
            imgutil.make_board(record={"fields": [
                {"name": "start", "size": 4},
                {"name": "start", "size": 4},
                {"name": "length", "size": 4},
            ]})


if __name__ == "__main__":
    unittest.main()
