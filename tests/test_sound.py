"""听感归一化与同音表测试。"""

import unittest

from confirm import HomophoneTable, base_sound_key, sound_key


class SoundKeyTests(unittest.TestCase):
    def test_case_and_whitespace_ignored(self):
        self.assertEqual(base_sound_key("Zhang Wei"), base_sound_key("zhangwei"))

    def test_punctuation_ignored(self):
        self.assertEqual(base_sound_key("123-4567"), base_sound_key("1234567"))
        self.assertEqual(base_sound_key("一二三四"), base_sound_key(" 一 二 三 四 "))

    def test_tone_marks_ignored(self):
        self.assertEqual(base_sound_key("zhāng"), base_sound_key("zhang"))
        self.assertEqual(base_sound_key("wěi"), base_sound_key("wei"))
        self.assertEqual(base_sound_key("lǜ"), base_sound_key("lv"))

    def test_tone_digits_ignored_only_after_letter(self):
        self.assertEqual(base_sound_key("zhang1wei3"), base_sound_key("zhangwei"))
        # 纯数字中的 1-5 不是声调，必须保留
        self.assertEqual(base_sound_key("1234"), "1234")
        self.assertEqual(base_sound_key("1500"), "1500")

    def test_non_string_rejected(self):
        with self.assertRaises(TypeError):
            base_sound_key(1234)  # type: ignore[arg-type]


class HomophoneTableTests(unittest.TestCase):
    def test_linked_spellings_share_key(self):
        table = HomophoneTable([("张伟", "张玮", "zhang wei")])
        keys = {sound_key(s, table) for s in ("张伟", "张玮", "zhang wei")}
        self.assertEqual(len(keys), 1)

    def test_unlinked_spellings_differ(self):
        table = HomophoneTable([("张伟", "张玮")])
        self.assertNotEqual(sound_key("张伟", table), sound_key("李娜", table))

    def test_transitive_merge(self):
        table = HomophoneTable()
        table.link("甲", "jia")
        table.link("jia", "佳")
        keys = {sound_key(s, table) for s in ("甲", "jia", "佳")}
        self.assertEqual(len(keys), 1)

    def test_canonical_is_deterministic(self):
        t1 = HomophoneTable([("b", "a", "c")])
        t2 = HomophoneTable([("c", "b", "a")])
        self.assertEqual(t1.canonical("c"), t2.canonical("c"))

    def test_empty_spelling_rejected(self):
        with self.assertRaises(ValueError):
            HomophoneTable([("...", "!!!")])


if __name__ == "__main__":
    unittest.main()
