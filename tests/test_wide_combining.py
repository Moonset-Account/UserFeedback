"""双宽字覆盖两格、整字清除与组合标记附着测试。"""

import unittest

from forensic_player import CellState, ForensicPlayer, WidthTable

WIDTHS = {"好": 2, "坏": 2, "A": 1, "́": 0, "̂": 0}
ACUTE = "́"  # U+0301 组合尖音符，UTF-8 为 CC 81
CIRCUMFLEX = "̂"  # U+0302 组合抑扬符，UTF-8 为 CC 82


class TestWideChars(unittest.TestCase):
    def setUp(self):
        self.player = ForensicPlayer(3, 8, WidthTable(WIDTHS))

    def test_wide_char_occupies_two_cells(self):
        p = self.player
        # 好 = E5 A5 BD，三字节
        data = "好".encode("utf-8")
        p.feed(data)
        snap = p.screen_at(3)
        left, right = snap.cell(0, 0), snap.cell(0, 1)
        self.assertEqual(left.state, CellState.CONTENT)
        self.assertEqual(left.base, "好")
        self.assertEqual(left.last_write_range, (0, 3))
        self.assertFalse(left.is_continuation)
        self.assertTrue(right.is_continuation)
        self.assertEqual(right.last_write_range, (0, 3))
        # 每格渲染一个字符位置：好 + 右半格空格 + 6 个空白格
        self.assertEqual(snap.line(0), "好       ")
        self.assertEqual(snap.cursor, (0, 2))

    def test_overwrite_left_cell_clears_whole_wide_char(self):
        p = self.player
        p.feed("好A".encode("utf-8"))  # 好占 0,1；A 占 2
        # 回到第 0 列写 X，应清掉整个“好”
        p.feed(b"\x1b[1;1HX")
        snap = p.screen_at(p.total_bytes)
        # 格 0：新内容 X；格 1：被覆盖动作清空（BLANK）；格 2：A 仍在
        self.assertEqual(snap.line(0), "X A     ")
        self.assertEqual(snap.cell(0, 0).base, "X")
        blank = snap.cell(0, 1)
        self.assertEqual(blank.state, CellState.BLANK)
        self.assertTrue(blank.is_erased)
        # 清空来源是 X 的写入动作（EL 之外的覆盖也算一次实际擦除）
        x_start = p.total_bytes - 1
        self.assertEqual(blank.clear_range, (x_start, x_start + 1))
        self.assertEqual(snap.cell(0, 2).base, "A")

    def test_overwrite_continuation_cell_clears_whole_wide_char(self):
        p = self.player
        p.feed("好".encode("utf-8"))  # 占 0,1
        # 定位到第 2 列（右半格）写 X
        p.feed(b"\x1b[1;2HX")
        snap = p.screen_at(p.total_bytes)
        self.assertEqual(snap.cell(0, 0).state, CellState.BLANK)
        self.assertEqual(snap.cell(0, 1).base, "X")
        # 左格的清除来源同样是 X 的写入区间
        x_start = p.total_bytes - 1
        self.assertEqual(
            snap.cell(0, 0).clear_range, (x_start, x_start + 1)
        )

    def test_wide_over_wide_replaces_both_cells(self):
        p = self.player
        p.feed("好".encode("utf-8"))
        p.feed(b"\x1b[1;1H")
        p.feed("坏".encode("utf-8"))
        snap = p.screen_at(p.total_bytes)
        self.assertEqual(snap.cell(0, 0).base, "坏")
        self.assertTrue(snap.cell(0, 1).is_continuation)
        self.assertEqual(snap.line(0), "坏       ")

    def test_wide_overlapping_adjacent_wide_chars(self):
        # 4 列屏幕：先在 col0 写“好”(0,1)，再 CUP 到 col2 写“坏”，
        # 两个双宽字相邻不冲突；再回 col1 写“坏”，需同时清掉两字。
        p = ForensicPlayer(1, 4, WidthTable(WIDTHS))
        p.feed("好".encode("utf-8"))
        p.feed(b"\x1b[1;3H")
        p.feed("坏".encode("utf-8"))
        snap = p.screen_at(p.total_bytes)
        self.assertEqual(snap.cell(0, 0).base, "好")
        self.assertEqual(snap.cell(0, 2).base, "坏")
        # 定位 col1（第一个字的右半），写双宽字覆盖 col1,2
        p.feed(b"\x1b[1;2H")
        p.feed("坏".encode("utf-8"))
        snap = p.screen_at(p.total_bytes)
        # 旧“好”整对被清；col1,2 是新“坏”；旧“坏”的右半 col3 被清
        self.assertEqual(snap.cell(0, 0).state, CellState.BLANK)
        self.assertEqual(snap.cell(0, 1).base, "坏")
        self.assertTrue(snap.cell(0, 2).is_continuation)
        self.assertEqual(snap.cell(0, 3).state, CellState.BLANK)

    def test_wide_char_clipped_at_last_column(self):
        # 屏幕宽 3，光标在末列时双宽字放不下，按单宽放末列
        p = ForensicPlayer(2, 3, WidthTable(WIDTHS))
        p.feed(b"ab")
        p.feed("好".encode("utf-8"))
        snap = p.screen_at(p.total_bytes)
        self.assertEqual(snap.cell(0, 2).base, "好")
        self.assertFalse(snap.cell(0, 2).is_continuation)
        self.assertEqual(snap.line(0), "ab好")
        # 末列写满后光标停在末列
        self.assertEqual(snap.cursor, (0, 2))

    def test_el_clears_wide_pair(self):
        p = self.player
        # 好占 0,1，再写 XY 占 2,3
        p.feed("好XY".encode("utf-8"))
        # 定位到左半格 col=0 再 EL0，擦除 0..7，整个双宽字被清
        p.feed(b"\x1b[1;1H\x1b[K")
        snap = p.screen_at(p.total_bytes)
        self.assertEqual(snap.cell(0, 0).state, CellState.BLANK)
        self.assertEqual(snap.cell(0, 1).state, CellState.BLANK)
        self.assertEqual(snap.cell(0, 2).state, CellState.BLANK)


class TestCombiningMarks(unittest.TestCase):
    def setUp(self):
        self.player = ForensicPlayer(2, 8, WidthTable(WIDTHS))

    def test_mark_attaches_to_base_and_keeps_byte_source(self):
        p = self.player
        # A(1 字节) + 组合尖音符 U+0301 (CC 81，2 字节)
        data = b"A" + ACUTE.encode("utf-8")
        self.assertEqual(len(data), 3)
        p.feed(data)
        snap = p.screen_at(3)
        cell = snap.cell(0, 0)
        self.assertEqual(cell.base, "A")
        self.assertEqual(len(cell.marks), 1)
        mark_char, mark_start, mark_end = cell.marks[0]
        self.assertEqual(mark_char, ACUTE)
        self.assertEqual((mark_start, mark_end), (1, 3))
        self.assertEqual(cell.text, "A" + ACUTE)
        # 标记不占新格，光标仍在第 1 列
        self.assertEqual(snap.cursor, (0, 1))

    def test_multiple_marks_keep_individual_ranges(self):
        p = self.player
        data = (
            b"A"
            + ACUTE.encode("utf-8")       # 字节 1..3
            + CIRCUMFLEX.encode("utf-8")  # 字节 3..5
        )
        p.feed(data)
        snap = p.screen_at(len(data))
        cell = snap.cell(0, 0)
        self.assertEqual(len(cell.marks), 2)
        self.assertEqual(cell.marks[0], (ACUTE, 1, 3))
        self.assertEqual(cell.marks[1], (CIRCUMFLEX, 3, 5))
        self.assertEqual(cell.text, "A" + ACUTE + CIRCUMFLEX)

    def test_orphan_mark_on_blank_cell_is_distinguishable(self):
        # 行首直接来组合标记：格不再是 VIRGIN，但没有基字
        p = self.player
        data = ACUTE.encode("utf-8")
        p.feed(data)
        snap = p.screen_at(len(data))
        cell = snap.cell(0, 0)
        self.assertEqual(cell.state, CellState.BLANK)
        self.assertIsNone(cell.base)
        self.assertEqual(len(cell.marks), 1)
        self.assertTrue(cell.ever_written)
        self.assertEqual(snap.line(0), "        ")  # 渲染仍为空白

    def test_overwriting_base_drops_old_marks(self):
        p = self.player
        p.feed(b"A" + ACUTE.encode("utf-8"))
        p.feed(b"\rB")
        snap = p.screen_at(p.total_bytes)
        cell = snap.cell(0, 0)
        self.assertEqual(cell.base, "B")
        self.assertEqual(cell.marks, ())


if __name__ == "__main__":
    unittest.main()
