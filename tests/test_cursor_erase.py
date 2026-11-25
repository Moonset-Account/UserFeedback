"""光标绝对定位 CUP 与行末擦除 EL 测试。"""

import unittest

from forensic_player import CellState, ForensicPlayer, WidthTable


class TestCupAndErase(unittest.TestCase):
    def setUp(self):
        self.player = ForensicPlayer(4, 8, WidthTable({}))

    def test_cup_basic_positioning(self):
        # ESC[2;3H -> 1 基 (2,3)，即 0 基 (1,2)
        self.player.feed(b"\x1b[2;3H")
        actions = self.player.actions
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].start, 0)
        self.assertEqual(actions[0].end, 6)
        snap = self.player.screen_at(6)
        self.assertEqual(snap.cursor, (1, 2))

    def test_cup_single_param_defaults_column_to_one(self):
        self.player.feed(b"\x1b[3H")
        snap = self.player.screen_at(4)
        self.assertEqual(snap.cursor, (2, 0))

    def test_cup_empty_params_default_one(self):
        self.player.feed(b"\x1b[;H")
        snap = self.player.screen_at(4)
        self.assertEqual(snap.cursor, (0, 0))

    def test_cup_clamped_to_screen(self):
        self.player.feed(b"\x1b[99;99H")
        snap = self.player.screen_at(8)
        self.assertEqual(snap.cursor, (3, 7))

        self.player.feed(b"\x1b[0;0H")
        snap = self.player.screen_at(8 + 6)
        self.assertEqual(snap.cursor, (0, 0))

    def test_el_to_end_of_line(self):
        p = self.player
        p.feed(b"abcdefgh")
        p.feed(b"\x1b[2;1H")  # 移到第二行行首
        p.feed(b"XY")
        p.feed(b"\x1b[1;3H")  # 移到第一行第 3 列 (0 基 col=2)
        # EL0 从光标擦到行末，序列位于字节 8+6+2+6 = 22
        p.feed(b"\x1b[K")
        total = p.total_bytes
        snap = p.screen_at(total)

        self.assertEqual(snap.line(0), "ab      ")
        # 前两格仍是内容
        self.assertEqual(snap.cell(0, 0).state, CellState.CONTENT)
        self.assertEqual(snap.cell(0, 1).state, CellState.CONTENT)
        # 第 3 格起被实际擦除，与 VIRGIN 可区分
        erased = snap.cell(0, 2)
        self.assertEqual(erased.state, CellState.BLANK)
        self.assertTrue(erased.is_erased)
        self.assertIsNotNone(erased.clear_range)
        self.assertEqual(erased.clear_range, erased.last_write_range)
        # 第二行不受影响
        self.assertEqual(snap.line(1), "XY      ")
        self.assertEqual(snap.cell(1, 2).state, CellState.VIRGIN)

    def test_el_modes(self):
        p = self.player
        p.feed(b"abcdefgh")
        p.feed(b"\x1b[1;4H")  # 0 基 (0,3)
        # mode 1：擦行首到光标
        p.feed(b"\x1b[1K")
        snap = p.screen_at(p.total_bytes)
        self.assertEqual(snap.line(0), "    efgh")
        for c in range(4):
            self.assertEqual(snap.cell(0, c).state, CellState.BLANK)
        for c in range(4, 8):
            self.assertEqual(snap.cell(0, c).state, CellState.CONTENT)

        # mode 2：整行擦除
        p.feed(b"\x1b[2K")
        snap = p.screen_at(p.total_bytes)
        self.assertEqual(snap.line(0), "        ")
        for c in range(8):
            self.assertEqual(snap.cell(0, c).state, CellState.BLANK)

    def test_erased_then_rewritten_clears_clear_range(self):
        p = self.player
        p.feed(b"abc")
        p.feed(b"\x1b[1;1H\x1b[K")  # 整行擦到行末（光标在行首）
        p.feed(b"Z")
        snap = p.screen_at(p.total_bytes)
        cell = snap.cell(0, 0)
        self.assertEqual(cell.state, CellState.CONTENT)
        self.assertEqual(cell.base, "Z")
        self.assertIsNone(cell.clear_range)


if __name__ == "__main__":
    unittest.main()
