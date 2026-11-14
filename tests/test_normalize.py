"""域名规范化与标签比较的单元测试。"""

import unittest

from splitdns.normalize import normalize_name, split_labels, suffix_matches


class TestNormalizeName(unittest.TestCase):
    def test_strips_trailing_root_dot(self):
        self.assertEqual(normalize_name("www.example.com."), "www.example.com")
        self.assertEqual(normalize_name("www.example.com.."), "www.example.com")

    def test_case_insensitive(self):
        self.assertEqual(normalize_name("WWW.Example.COM"), "www.example.com")

    def test_root_domain_normalizes_to_empty(self):
        self.assertEqual(normalize_name("."), "")
        self.assertEqual(normalize_name(""), "")

    def test_idn_ascii_matches_unicode(self):
        puny = "内部".encode("idna").decode("ascii")
        self.assertTrue(puny.startswith("xn--"))
        self.assertEqual(normalize_name("mail.%s.example" % puny),
                         "mail.内部.example")

    def test_unicode_side_normalized_consistently(self):
        self.assertEqual(normalize_name("MAIL.内部.Example"),
                         "mail.内部.example")

    def test_invalid_punycode_kept_as_ascii(self):
        # 非法 punycode 不应抛错,按普通标签处理
        self.assertEqual(normalize_name("xn--.example"), "xn--.example")

    def test_non_string_rejected(self):
        with self.assertRaises(ValueError):
            normalize_name(None)
        with self.assertRaises(ValueError):
            normalize_name(123)


class TestSplitLabels(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(split_labels("a.b.example.com"),
                         ["a", "b", "example", "com"])

    def test_root_has_no_labels(self):
        self.assertEqual(split_labels("."), [])


class TestSuffixMatches(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(suffix_matches("corp.example.com", "corp.example.com"))

    def test_subdomain_match(self):
        self.assertTrue(suffix_matches("www.corp.example.com",
                                       "corp.example.com"))

    def test_label_boundary_not_substring(self):
        self.assertFalse(suffix_matches("notcorp.example.com",
                                        "corp.example.com"))

    def test_shorter_query_does_not_match(self):
        self.assertFalse(suffix_matches("example.com", "corp.example.com"))

    def test_case_and_root_dot_ignored(self):
        self.assertTrue(suffix_matches("WWW.Corp.Example.Com.",
                                       "CORP.example.com."))

    def test_idn_match_both_directions(self):
        puny = "内部".encode("idna").decode("ascii")
        self.assertTrue(suffix_matches("mail.%s.example" % puny,
                                       "内部.example"))
        self.assertTrue(suffix_matches("mail.内部.example",
                                       "%s.example" % puny))


if __name__ == "__main__":
    unittest.main()
