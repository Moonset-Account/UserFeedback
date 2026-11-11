"""版本解析与排序规则测试（三段式数字版本 + 预发布规则）。"""

import unittest

from adjudicator.versioning import (
    IncomparableVersionError,
    Version,
    compare,
    parse_version,
)


class ParseVersionTest(unittest.TestCase):
    def test_valid_three_segment_versions(self):
        for text in ("1.0.0", "0.0.0", "10.20.30", "1.2.3-rc.1",
                     "1.0.0-alpha.beta.11", "2.0.0+build.42"):
            with self.subTest(text=text):
                self.assertIsInstance(parse_version(text), Version)

    def test_invalid_versions_return_none(self):
        # 两段、四段、前导零、日期式版本、通配符、空串均不可解析
        for text in ("1.2", "1.2.3.4", "01.2.3", "1.02.0", "1.2.03",
                     "20261109-r1", "1.*.0", "v1.2.3", "", "1.2.3-",
                     "1.2.3-alpha..1", "1.2.3-01", "1.2.3+", None):
            with self.subTest(text=text):
                self.assertIsNone(parse_version(text))

    def test_build_metadata_ignored_but_preserved(self):
        a = parse_version("1.0.0+exp.sha.5114f85")
        b = parse_version("1.0.0")
        self.assertEqual(compare(a, b), 0)
        self.assertEqual(a.raw, "1.0.0+exp.sha.5114f85")

    def test_numeric_prerelease_no_leading_zero(self):
        self.assertIsNone(parse_version("1.0.0-rc.01"))
        self.assertIsNotNone(parse_version("1.0.0-rc.0"))


class OrderingTest(unittest.TestCase):
    def test_core_numeric_ordering(self):
        ordered = ["1.0.0", "1.0.1", "1.1.0", "1.2.0", "2.0.0"]
        versions = [parse_version(v) for v in ordered]
        for low, high in zip(versions, versions[1:]):
            self.assertEqual(compare(low, high), -1)
            self.assertLess(low, high)

    def test_release_higher_than_prerelease(self):
        release = parse_version("1.0.0")
        pre = parse_version("1.0.0-rc.1")
        self.assertGreater(release, pre)
        self.assertLess(pre, release)

    def test_semver_prerelease_chain(self):
        # SemVer 2.0.0 官方示例链
        chain = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
        ]
        versions = [parse_version(v) for v in chain]
        for low, high in zip(versions, versions[1:]):
            with self.subTest(low=low.raw, high=high.raw):
                self.assertLess(low, high)

    def test_numeric_identifier_lower_than_alphanumeric(self):
        self.assertLess(parse_version("1.0.0-1"), parse_version("1.0.0-rc"))

    def test_equal_versions(self):
        self.assertEqual(compare(parse_version("1.2.3-rc.1"),
                                 parse_version("1.2.3-rc.1")), 0)

    def test_comparison_against_non_version_raises(self):
        with self.assertRaises(IncomparableVersionError):
            compare(parse_version("1.0.0"), None)  # type: ignore[arg-type]
        with self.assertRaises(IncomparableVersionError):
            parse_version("1.0.0") < "1.0.1"


if __name__ == "__main__":
    unittest.main()
