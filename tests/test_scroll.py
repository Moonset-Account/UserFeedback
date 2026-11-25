"""有限行数上卷（scroll）测试。"""

import unittest

from forensic_player import CellState, ForensicPlayer, WidthTable


class TestScroll(unittest.TestCase):
    def test_scroll_drops_top_and_appends_virgin_row(self):
        # 3 行 4 列。LF 保留列，因此每行写完后用 CR 回到第 0 列。
        p = ForensicPlayer(3, 4, WidthTable({}))
        p.feed(b"aaa\r\n")   # 第 0 行 aaa
        p.feed(b"bbb\r\n")   # 第 1 行
        # 写底行后不再 LF；显式再发一个 LF 触发上卷
        p.feed(b"ccc")
        p.feed(b"\n")
        snap = p.screen_at(p.total_bytes)

        self.assertEqual(snap.line(0), "bbb ")
        self.assertEqual(snap.line(1), "ccc ")
        # 新底行从未写入
        for c in range(4):
            self.assertEqual(snap.cell(2, c).state, CellState.VIRGIN)
        # 被丢掉的 aaa 无法再访问（屏幕只有 3 行）
        self.assertEqual(snap.rows, 3)

    def test_scroll_preserves_provenance_of_remaining_rows(self):
        p = ForensicPlayer(2, 3, WidthTable({}))
        p.feed(b"abc")          # 字节 0..2，光标停在 col3（末列）
        p.feed(b"\r\n")         # CR 回 col0，再 LF 到第 1 行
        p.feed(b"XY")           # 位于底行 col0..1
        p.feed(b"\n")           # 触发上卷
        snap = p.screen_at(p.total_bytes)
        # 原底行内容上移到第 0 行，字节来源保持不变
        self.assertEqual(snap.cell(0, 0).base, "X")
        self.assertEqual(snap.cell(0, 0).last_write_range, (5, 6))
        self.assertEqual(snap.cell(0, 1).base, "Y")
        self.assertEqual(snap.cell(0, 1).last_write_range, (6, 7))

    def test_repeated_scroll(self):
        p = ForensicPlayer(2, 2, WidthTable({}))
        # a、b 写满两页；写 c 时在底行触发一次上卷，写 d 再触发一次。
        for ch in "ab":
            p.feed(ch.encode("utf-8"))
            p.feed(b"\r\n")
        p.feed(b"c")   # 底行（上卷后）
        p.feed(b"\r\n")  # 再上卷一次，新底行
        p.feed(b"d")
        snap = p.screen_at(p.total_bytes)
        self.assertEqual(snap.line(0), "c ")
        self.assertEqual(snap.line(1), "d ")


if __name__ == "__main__":
    unittest.main()
