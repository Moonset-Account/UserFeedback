"""宽度表与解析器的独立单元测试。"""

import unittest

from forensic_player import WidthTable
from forensic_player.stream_parser import (
    ActionType,
    StreamParser,
    parse_bytes,
)


class TestWidthTable(unittest.TestCase):
    def test_default_width_and_lookup(self):
        table = WidthTable({"好": 2, "é": 0})
        self.assertEqual(table.width_of("好"), 2)
        self.assertEqual(table.width_of("é"), 0)
        self.assertEqual(table.width_of("x"), 1)  # 未登记默认 1
        self.assertTrue(table.is_wide("好"))
        self.assertTrue(table.is_combining("é"))
        self.assertIn("好", table)

    def test_custom_default_width(self):
        table = WidthTable({}, default_width=2)
        self.assertEqual(table.width_of("x"), 2)

    def test_invalid_construction(self):
        with self.assertRaises(ValueError):
            WidthTable({"ab": 1})
        with self.assertRaises(ValueError):
            WidthTable({"x": 3})
        with self.assertRaises(ValueError):
            WidthTable({}, default_width=5)

    def test_mapping_and_iterable_inputs_equivalent(self):
        a = WidthTable({"好": 2})
        b = WidthTable([("好", 2)])
        self.assertEqual(a.width_of("好"), b.width_of("好"))


class TestStreamParser(unittest.TestCase):
    def test_parse_mixed_actions(self):
        actions = parse_bytes(b"a\x08\x0d\x0ab")
        types = [a.type for a in actions]
        self.assertEqual(
            types,
            [
                ActionType.PRINT,
                ActionType.BACKSPACE,
                ActionType.CARRIAGE_RETURN,
                ActionType.LINE_FEED,
                ActionType.PRINT,
            ],
        )
        self.assertEqual(actions[0].char, "a")
        self.assertEqual((actions[1].start, actions[1].end), (1, 2))

    def test_cup_action_fields(self):
        actions = parse_bytes(b"\x1b[3;7H")
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertEqual(action.type, ActionType.CUP)
        self.assertEqual((action.start, action.end), (0, 6))
        self.assertEqual((action.row, action.col), (3, 7))

    def test_el_action_fields(self):
        actions = parse_bytes(b"\x1b[2K")
        self.assertEqual(actions[0].type, ActionType.EL)
        self.assertEqual(actions[0].mode, 2)
        # 缺省模式为 0
        self.assertEqual(parse_bytes(b"\x1b[K")[0].mode, 0)

    def test_pending_escape_flushed_by_later_fragment(self):
        parser = StreamParser()
        first = parser.feed(b"x\x1b")
        # x 已完成；ESC 留在 pending 缓冲区
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].char, "x")
        self.assertEqual(parser.pending_bytes, 1)
        actions = parser.feed(b"[K")
        # 本次只吐出 EL，其绝对区间覆盖完整 4 字节序列中的后 3 字节
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, ActionType.EL)
        self.assertEqual((actions[0].start, actions[0].end), (1, 4))
        self.assertEqual(parser.pending_bytes, 0)

    def test_malformed_csi_is_abandoned(self):
        # 中间字节 '!' 使 CSI 非法，其后的 K 作为普通文本
        parser = StreamParser()
        actions = parser.feed(b"\x1b[2!KZ")
        chars = [a.char for a in actions if a.type == ActionType.PRINT]
        # 非法 CSI 的 ESC [ 2 ! 被消耗，K 与 Z 作为普通打印
        self.assertEqual(chars, ["K", "Z"])

    def test_4byte_utf8(self):
        # 𝄞 U+1D11E = F0 9D 84 9E
        data = "\U0001d11e".encode("utf-8")
        self.assertEqual(len(data), 4)
        actions = parse_bytes(data)
        self.assertEqual(actions[0].char, "\U0001d11e")
        self.assertEqual((actions[0].start, actions[0].end), (0, 4))

    def test_surrogate_encoding_is_replacement(self):
        # ED A0 80 会解码出代理码点 U+D800，按 U+FFFD 处理
        actions = parse_bytes(b"\xed\xa0\x80")
        self.assertEqual(actions[0].char, "�")
        self.assertEqual((actions[0].start, actions[0].end), (0, 3))


if __name__ == "__main__":
    unittest.main()
