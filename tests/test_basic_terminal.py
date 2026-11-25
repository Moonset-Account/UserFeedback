"""基础文本打印、CR/LF/退格与字节来源区间测试。"""

import unittest

from forensic_player import CellState, ForensicPlayer, WidthTable


def make_player(rows=4, cols=10, widths=None):
    return ForensicPlayer(rows, cols, WidthTable(widths or {}))


class TestPrintAndProvenance(unittest.TestCase):
    def test_ascii_print_and_ranges(self):
        player = make_player()
        player.feed(b"abc")
        snap = player.screen_at(3)

        self.assertEqual(snap.line(0), "abc       ")
        self.assertEqual(snap.cursor, (0, 3))
        for col, byte_index in enumerate((0, 1, 2)):
            cell = snap.cell(0, col)
            self.assertEqual(cell.state, CellState.CONTENT)
            self.assertEqual(cell.base, chr(ord("a") + col))
            self.assertEqual(cell.last_write_range, (byte_index, byte_index + 1))
            self.assertIsNone(cell.clear_range)
            self.assertTrue(cell.ever_written)

    def test_virgin_cells_never_written(self):
        player = make_player()
        player.feed(b"abc")
        cell = player.cell_at(3, 0, 5)
        self.assertEqual(cell.state, CellState.VIRGIN)
        self.assertFalse(cell.ever_written)
        self.assertIsNone(cell.last_write_range)
        self.assertIsNone(cell.clear_range)
        self.assertEqual(cell.text, "")

    def test_carriage_return_overwrite(self):
        # "ab\rX"：CR 回到行首，X 覆盖 a，原 b 保留
        player = make_player()
        player.feed(b"ab\rX")
        snap = player.screen_at(4)
        self.assertEqual(snap.line(0), "Xb        ")
        first = snap.cell(0, 0)
        self.assertEqual(first.base, "X")
        # X 是流中第 3 个字节
        self.assertEqual(first.last_write_range, (3, 4))

    def test_line_feed_keeps_column(self):
        player = make_player()
        player.feed(b"ab\ncd")
        snap = player.screen_at(5)
        self.assertEqual(snap.line(0), "ab        ")
        self.assertEqual(snap.line(1), "  cd      ")
        self.assertEqual(snap.cursor, (1, 4))
        # LF 字节来源是索引 2
        self.assertEqual(snap.cell(1, 2).last_write_range, (3, 4))

    def test_crlf_together(self):
        player = make_player()
        player.feed(b"ab\r\ncd")
        snap = player.screen_at(6)
        # CR 先把列归零，再 LF，第二行从第 0 列开始
        self.assertEqual(snap.line(0), "ab        ")
        self.assertEqual(snap.line(1), "cd        ")
        self.assertEqual(snap.cursor, (1, 2))

    def test_backspace_moves_and_overwrite(self):
        player = make_player()
        player.feed(b"abc\bX")
        snap = player.screen_at(5)
        self.assertEqual(snap.line(0), "abX       ")
        cell = snap.cell(0, 2)
        self.assertEqual(cell.base, "X")
        self.assertEqual(cell.last_write_range, (4, 5))

    def test_backspace_at_column_zero_is_noop(self):
        player = make_player()
        player.feed(b"\bX")
        snap = player.screen_at(2)
        self.assertEqual(snap.cursor, (0, 1))
        self.assertEqual(snap.cell(0, 0).base, "X")

    def test_multibyte_utf8_ranges(self):
        # 中 = E4 B8 AD，三字节；文 = E6 96 87
        player = make_player()
        data = "中文".encode("utf-8")
        self.assertEqual(len(data), 6)
        player.feed(data)
        snap = player.screen_at(6)
        self.assertEqual(snap.cell(0, 0).base, "中")
        self.assertEqual(snap.cell(0, 0).last_write_range, (0, 3))
        self.assertEqual(snap.cell(0, 1).base, "文")
        self.assertEqual(snap.cell(0, 1).last_write_range, (3, 6))


if __name__ == "__main__":
    unittest.main()
