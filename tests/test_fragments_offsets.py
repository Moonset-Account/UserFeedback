"""分片截在控制序列或多字节字符中的增量解析与按偏移查询测试。"""

import unittest

from forensic_player import CellState, ForensicPlayer, WidthTable


class TestFragmentation(unittest.TestCase):
    def test_split_inside_multibyte_char(self):
        # 中 = E4 B8 AD
        data = "中".encode("utf-8")
        p1 = ForensicPlayer(2, 4, WidthTable({}))
        p1.feed(data[:1])          # 只有首字节
        self.assertEqual(p1.pending_bytes, 1)
        self.assertEqual(p1.actions, ())
        # 截在半个字符处：查询不显示任何已完成动作
        snap = p1.screen_at(1)
        self.assertEqual(snap.cell(0, 0).state, CellState.VIRGIN)

        p1.feed(data[1:])          # 补齐
        snap = p1.screen_at(3)
        self.assertEqual(snap.cell(0, 0).base, "中")
        self.assertEqual(snap.cell(0, 0).last_write_range, (0, 3))

    def test_split_inside_csi_sequence(self):
        seq = b"\x1b[2;2H"
        p = ForensicPlayer(2, 4, WidthTable({}))
        # 逐字节喂入
        for b in seq[:-1]:
            p.feed(bytes([b]))
            self.assertEqual(p.actions, ())
        self.assertEqual(p.pending_bytes, len(seq) - 1)
        # 半截序列期间查询，光标仍在原点
        self.assertEqual(p.screen_at(len(seq) - 1).cursor, (0, 0))
        p.feed(seq[-1:])
        self.assertEqual(p.screen_at(len(seq)).cursor, (1, 1))
        action = p.actions[0]
        self.assertEqual((action.start, action.end), (0, len(seq)))

    def test_csi_split_across_three_fragments(self):
        p = ForensicPlayer(2, 4, WidthTable({}))
        p.feed(b"\x1b[2")
        p.feed(b";3")
        p.feed(b"Hab")
        snap = p.screen_at(p.total_bytes)
        self.assertEqual(snap.cursor, (1, 3))  # 定位 (1,2) 后写 a,b
        self.assertEqual(snap.cell(1, 2).base, "a")

    def test_truncated_csi_never_completes(self):
        p = ForensicPlayer(2, 4, WidthTable({}))
        p.feed(b"ab\x1b[2")     # 流结束时 CSI 仍半截
        snap = p.screen_at(p.total_bytes)
        # 已完成的打印动作生效，半截序列不生效
        self.assertEqual(snap.line(0), "ab  ")
        self.assertEqual(p.pending_bytes, 3)

    def test_invalid_utf8_emits_replacement(self):
        p = ForensicPlayer(2, 4, WidthTable({}))
        # FF 是非法首字节；后面跟一个合法 A
        p.feed(b"\xffA")
        snap = p.screen_at(2)
        self.assertEqual(snap.cell(0, 0).base, "�")
        self.assertEqual(snap.cell(0, 0).last_write_range, (0, 1))
        self.assertEqual(snap.cell(0, 1).base, "A")

    def test_truncated_multibyte_at_end_stays_pending(self):
        p = ForensicPlayer(2, 4, WidthTable({}))
        p.feed(b"\xe4\xb8")     # “中”的前两个字节
        self.assertEqual(p.pending_bytes, 2)
        snap = p.screen_at(2)
        self.assertEqual(snap.cell(0, 0).state, CellState.VIRGIN)

    def test_unsupported_escape_sequences_are_ignored(self):
        p = ForensicPlayer(2, 4, WidthTable({}))
        # ESC c（RIS）不在支持范围内：两字节静默跳过
        p.feed(b"\x1bcAB")
        snap = p.screen_at(p.total_bytes)
        self.assertEqual(snap.cell(0, 0).base, "A")
        self.assertEqual(snap.cell(0, 1).base, "B")
        # 不支持的 CSI 终结符同样跳过
        p.feed(b"\x1b[2JCD")
        snap = p.screen_at(p.total_bytes)
        self.assertEqual(snap.cell(0, 2).base, "C")
        self.assertEqual(snap.cell(0, 3).base, "D")

    def test_unsupported_c0_controls_ignored(self):
        p = ForensicPlayer(2, 4, WidthTable({}))
        p.feed(b"A\x07B")       # BEL 无动作
        snap = p.screen_at(3)
        self.assertEqual(snap.cell(0, 0).base, "A")
        self.assertEqual(snap.cell(0, 1).base, "B")
        self.assertEqual(snap.cursor, (0, 2))


class TestOffsetSemantics(unittest.TestCase):
    def test_offset_zero_is_empty_screen(self):
        p = ForensicPlayer(2, 4, WidthTable({}))
        p.feed(b"abc")
        snap = p.screen_at(0)
        self.assertEqual(snap.cursor, (0, 0))
        for c in range(4):
            self.assertEqual(snap.cell(0, c).state, CellState.VIRGIN)

    def test_offset_boundaries_include_fully_completed_actions(self):
        p = ForensicPlayer(2, 4, WidthTable({}))
        data = "中".encode("utf-8")  # 3 字节
        p.feed(data)
        # 偏移 1、2：动作 end=3 尚未到达，不显示
        for off in (1, 2):
            self.assertEqual(
                p.screen_at(off).cell(0, 0).state, CellState.VIRGIN
            )
        # 偏移 3：动作完整，显示
        self.assertEqual(p.screen_at(3).cell(0, 0).base, "中")

    def test_offset_out_of_range_raises(self):
        p = ForensicPlayer(2, 4, WidthTable({}))
        p.feed(b"ab")
        with self.assertRaises(ValueError):
            p.screen_at(-1)
        with self.assertRaises(ValueError):
            p.screen_at(3)

    def test_byte_by_byte_equivalent_to_one_shot(self):
        """交错分片的关键不变量：逐字节喂入与一次性喂入，在每个偏移
        上得到完全相同的屏幕与来源区间。"""
        stream = (
            b"ab\r\ncd\bX"
            + b"\x1b[2;2H"
            + "好".encode("utf-8")
            + b"\x1b[KY"
            + "́".encode("utf-8")
        )
        widths = {"好": 2, "́": 0}
        whole = ForensicPlayer(3, 6, WidthTable(widths))
        whole.feed(stream)

        split = ForensicPlayer(3, 6, WidthTable(widths))
        for i, byte in enumerate(stream):
            split.feed(bytes([byte]))
            # 每次喂入后两边在当前总偏移处必须一致
            off = i + 1
            a = whole.screen_at(off)
            b = split.screen_at(off)
            self.assertEqual(a.text(), b.text(), f"offset={off} 文本不一致")
            self.assertEqual(a.cursor, b.cursor, f"offset={off} 光标不一致")
            for r in range(3):
                for c in range(6):
                    ca, cb = a.cell(r, c), b.cell(r, c)
                    self.assertEqual(
                        ca.state, cb.state, f"offset={off} ({r},{c}) 状态"
                    )
                    self.assertEqual(
                        ca.last_write_range,
                        cb.last_write_range,
                        f"offset={off} ({r},{c}) 来源",
                    )
                    self.assertEqual(
                        ca.clear_range,
                        cb.clear_range,
                        f"offset={off} ({r},{c}) 擦除区间",
                    )
                    self.assertEqual(ca.marks, cb.marks)

    def test_interleaved_fragments_absolute_offsets(self):
        """分片边界同时切过多字节字符和控制序列时，绝对偏移仍正确。"""
        p = ForensicPlayer(2, 6, WidthTable({"好": 2}))
        zhong = "好".encode("utf-8")
        p.feed(b"ab" + zhong[:1])          # 截在双宽字首字节
        p.feed(zhong[1:] + b"\x1b[2")      # 补齐字符，又截在 CSI 中
        p.feed(b";2HX")                    # 补齐 CSI 后打印
        snap = p.screen_at(p.total_bytes)
        self.assertEqual(snap.cell(0, 2).base, "好")
        self.assertEqual(snap.cell(0, 2).last_write_range, (2, 5))
        # CUP 完整区间从 ESC(5) 到 H(10)
        cup = next(a for a in p.actions if a.type.value == "cup")
        self.assertEqual((cup.start, cup.end), (5, 11))
        self.assertEqual(snap.cell(1, 1).base, "X")


if __name__ == "__main__":
    unittest.main()
