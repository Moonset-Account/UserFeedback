"""题面强调的交错边界补充测试。"""

import unittest

from forensic_player import CellState, ForensicPlayer, WidthTable

ACUTE = "́"
WIDTHS = {"好": 2, "坏": 2, ACUTE: 0, "̂": 0}


class TestExtraEdgeCases(unittest.TestCase):
    def test_combining_mark_on_wide_base_uses_left_two_columns(self):
        p = ForensicPlayer(2, 6, WidthTable(WIDTHS))
        # 好占 col0,1，光标到 col2；零宽标记应附着到 col0 的基字
        p.feed("好".encode("utf-8") + ACUTE.encode("utf-8"))
        snap = p.screen_at(p.total_bytes)
        left = snap.cell(0, 0)
        right = snap.cell(0, 1)
        self.assertEqual(left.base, "好")
        self.assertEqual(len(left.marks), 1)
        self.assertEqual(left.marks[0][0], ACUTE)
        # 标记的字节来源独立于基字
        self.assertEqual(left.last_write_range, (3, 5))
        # 右半格不持有标记
        self.assertEqual(right.marks, ())
        self.assertTrue(right.is_continuation)
        # 零宽标记不移动光标
        self.assertEqual(snap.cursor, (0, 2))

    def test_orphan_mark_then_base_attaches_mark_to_later_base(self):
        p = ForensicPlayer(2, 4, WidthTable(WIDTHS))
        # 行首孤立标记先落在 col0（BLANK）；随后打印 A，标记按“光标左侧
        # 基字”规则附着到 A 所在的 col0。
        p.feed(ACUTE.encode("utf-8") + b"A")
        snap = p.screen_at(p.total_bytes)
        cell = snap.cell(0, 0)
        self.assertEqual(cell.base, "A")
        self.assertEqual(len(cell.marks), 1)
        self.assertEqual(cell.marks[0][0], ACUTE)
        self.assertEqual(snap.cell(0, 1).state, CellState.VIRGIN)

    def test_orphan_mark_alone_stays_blank(self):
        # 只有孤立标记、没有后续基字时，col0 保持 BLANK 并持有标记
        p = ForensicPlayer(2, 4, WidthTable(WIDTHS))
        p.feed(ACUTE.encode("utf-8"))
        snap = p.screen_at(p.total_bytes)
        cell = snap.cell(0, 0)
        self.assertEqual(cell.state, CellState.BLANK)
        self.assertIsNone(cell.base)
        self.assertEqual(len(cell.marks), 1)

    def test_el_byte_range_is_the_full_csi_sequence(self):
        p = ForensicPlayer(2, 4, WidthTable({}))
        p.feed(b"abcd")          # 0..3
        p.feed(b"\x1b[1;1H")     # 4..9（6 字节），CUP
        p.feed(b"\x1b[K")        # 10..12（3 字节），EL0 擦整行
        snap = p.screen_at(p.total_bytes)
        for c in range(4):
            cell = snap.cell(0, c)
            self.assertEqual(cell.state, CellState.BLANK)
            self.assertEqual(cell.clear_range, (10, 13))
            self.assertEqual(cell.last_write_range, (10, 13))

    def test_wide_clear_range_attributes_rewrite_action(self):
        p = ForensicPlayer(1, 4, WidthTable(WIDTHS))
        p.feed("好".encode("utf-8"))   # 0..2，占 col0,1
        p.feed(b"X")                   # 3，写在 col2，不影响双宽字
        p.feed(b"\x1b[1;1HY")          # CUP(4..9) + Y(10) 覆盖 col0
        snap = p.screen_at(p.total_bytes)
        self.assertEqual(snap.cell(0, 0).base, "Y")
        blank_right = snap.cell(0, 1)
        self.assertEqual(blank_right.state, CellState.BLANK)
        # 清除旧双宽字的来源是 Y 的写入动作，而不是 CUP
        self.assertEqual(blank_right.clear_range, (10, 11))
        # col2 的 X 完好
        self.assertEqual(snap.cell(0, 2).base, "X")

    def test_end_to_end_interleaved_recording(self):
        """综合场景：普通字/双宽/组合标记/CR/BS/CUP/EL/上卷/分片交错。"""
        widths = {"好": 2, ACUTE: 0}
        stream = (
            b"ab"
            + "好".encode("utf-8")          # 2..4
            + ACUTE.encode("utf-8")         # 5..6，标记附着到“好”
            + b"\rX"                        # CR 回行首，X 覆盖 a -> 清双宽字
            + b"\nY"                        # LF 到第二行（列保留为 1）
            + b"\x1b[1;1H"                  # 回第一行行首
            + b"\x1b[K"                     # 擦第一行
        )
        # 一次性解析作为基准
        whole = ForensicPlayer(3, 6, WidthTable(widths))
        whole.feed(stream)

        # 按不规则分片喂入
        split = ForensicPlayer(3, 6, WidthTable(widths))
        cuts = [1, 2, 3, 5, 6, 7, 8, 9, 10, 14, len(stream)]
        prev = 0
        for cut in cuts:
            split.feed(stream[prev:cut])
            prev = cut

        for off in range(len(stream) + 1):
            a = whole.screen_at(off)
            b = split.screen_at(off)
            self.assertEqual(a.text(), b.text(), f"offset {off} 文本")
            self.assertEqual(a.cursor, b.cursor, f"offset {off} 光标")
            for r in range(3):
                for c in range(6):
                    ca, cb = a.cell(r, c), b.cell(r, c)
                    self.assertEqual(
                        ca.last_write_range,
                        cb.last_write_range,
                        f"offset {off} ({r},{c}) 来源",
                    )
                    self.assertEqual(
                        ca.clear_range, cb.clear_range,
                        f"offset {off} ({r},{c}) 擦除",
                    )

        final = whole.screen_at(len(stream))
        # 第一行已被 EL 整行擦除（全部 BLANK，且区别于 VIRGIN）
        for c in range(6):
            self.assertEqual(final.cell(0, c).state, CellState.BLANK)
        # 第二行 col1 是 Y（LF 保留列）
        self.assertEqual(final.cell(1, 1).base, "Y")

    def test_render_and_text_consistency(self):
        p = ForensicPlayer(1, 5, WidthTable(WIDTHS))
        p.feed("好".encode("utf-8") + b"AB")
        snap = p.screen_at(p.total_bytes)
        # 5 个格：好(左) 空格(右半) A B 空格
        self.assertEqual(snap.text(), "好 AB ")
        self.assertEqual(len(snap.line(0)), 5)


if __name__ == "__main__":
    unittest.main()
