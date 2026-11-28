import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collation import Collation, SortKey


def make_collation() -> Collation:
    """两级权重的测试规则表。"""
    c = Collation(levels=2)
    c.add("a", ((1,), (1,)))
    c.add("e", ((2,), (1,)))
    c.add("c", ((3,), (1,)))
    c.add("h", ((4,), (2,)))
    c.add("ch", ((10,), (5,)))       # 收缩：两字符一个权重
    c.add("dz", ((10,), (9,)))       # 与 "ch" 主权重相同、次权重不同
    c.add("æ", ((1, 2), (1, 1)))     # 展开：单字符展开为 "ae" 的权重序列
    c.add("-", ((7,), ()))           # 次权重可忽略
    c.add(" ", ((), ()))             # 完全可忽略
    return c


class TestContraction(unittest.TestCase):
    def setUp(self):
        self.col = make_collation()

    def test_longest_match_contraction(self):
        # "ch" 作为整体匹配收缩规则，而不是 "c"+"h" 各自权重的拼接。
        self.assertEqual(self.col.sort_key("ch"), SortKey(((10,), (5,))))
        self.assertNotEqual(self.col.sort_key("ch"), SortKey(((3, 4), (1, 2))))

    def test_contraction_inside_longer_text(self):
        # "ach": a + 收缩 ch。
        self.assertEqual(self.col.sort_key("ach"), SortKey(((1, 10), (1, 5))))

    def test_contraction_spans_chunk_boundary(self):
        # 收缩序列 "ch" 被切到两个输入块中，仍应匹配收缩规则。
        builder = self.col.key_builder()
        builder.feed("c")
        builder.feed("h")
        self.assertEqual(builder.finish(), self.col.sort_key("ch"))

    def test_chunked_feed_matches_one_shot(self):
        text = "each dz-ch"
        for size in (1, 2, 3, len(text)):
            builder = self.col.key_builder()
            for i in range(0, len(text), size):
                builder.feed(text[i : i + size])
            self.assertEqual(
                builder.finish(),
                self.col.sort_key(text),
                f"chunk_size={size}",
            )

    def test_builder_reuse_after_finish_rejected(self):
        builder = self.col.key_builder()
        builder.feed("a")
        builder.finish()
        with self.assertRaises(RuntimeError):
            builder.feed("b")
        with self.assertRaises(RuntimeError):
            builder.finish()


class TestExpansionAndIgnorable(unittest.TestCase):
    def setUp(self):
        self.col = make_collation()

    def test_single_char_expansion(self):
        # "æ" 展开为两个主权重元素，与 "ae" 完全相等。
        self.assertEqual(self.col.sort_key("æ"), SortKey(((1, 2), (1, 1))))
        self.assertEqual(self.col.sort_key("æ"), self.col.sort_key("ae"))

    def test_fully_ignorable_char(self):
        self.assertEqual(self.col.sort_key("a b"), self.col.sort_key("ab"))

    def test_level_ignorable_weight(self):
        # "-" 只贡献主权重，次权重为空。
        self.assertEqual(self.col.sort_key("-"), SortKey(((7,), ())))
        self.assertEqual(self.col.sort_key("a-"), SortKey(((1, 7), (1,))))

    def test_default_weight_for_unknown_char(self):
        self.assertEqual(
            self.col.sort_key("q"), SortKey(((ord("q"),), ()))
        )


class TestComparison(unittest.TestCase):
    def setUp(self):
        self.col = make_collation()

    def test_primary_equal_then_secondary(self):
        # "ch" 与 "dz" 主权重都是 (10,)，由次权重 5 < 9 决定顺序。
        self.assertLess(self.col.sort_key("ch"), self.col.sort_key("dz"))
        self.assertEqual(
            sorted(["dz", "ch"], key=self.col.sort_key), ["ch", "dz"]
        )

    def test_whole_string_primary_before_secondary(self):
        # "cha" 次权重 (5,1) 并不小于 "dzb" 的 (9,)，
        # 但整串主权重 (10,1) < (10,98) 先决定结果。
        self.assertLess(self.col.sort_key("cha"), self.col.sort_key("dzb"))

    def test_equality_requires_all_levels(self):
        self.assertEqual(self.col.sort_key("ch"), self.col.sort_key("ch"))
        self.assertNotEqual(self.col.sort_key("ch"), self.col.sort_key("dz"))
        self.assertEqual(hash(self.col.sort_key("ch")), hash(self.col.sort_key("ch")))


if __name__ == "__main__":
    unittest.main()
