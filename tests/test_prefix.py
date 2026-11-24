"""Prefix 的规范化、包含判断与边界行为。"""

import unittest

from route_audit import InvalidPrefixError, Prefix


class TestPrefix(unittest.TestCase):
    def test_parse_and_normalize_host_bits(self) -> None:
        # 主机位非零也能接收，并规范化为网络地址
        p = Prefix("10.10.1.5/24")
        self.assertEqual(p.text, "10.10.1.0/24")
        self.assertEqual(p.prefix_length, 24)
        self.assertEqual(p.version, 4)

    def test_ipv6_supported(self) -> None:
        p = Prefix("2001:db8::/32")
        self.assertEqual(p.prefix_length, 32)
        self.assertEqual(p.version, 6)

    def test_contains_hierarchy(self) -> None:
        outer = Prefix("10.10.0.0/16")
        inner = Prefix("10.10.1.0/24")
        self.assertTrue(outer.contains(inner))
        self.assertTrue(outer.contains(outer))
        self.assertFalse(inner.contains(outer))

    def test_contains_disjoint_and_family_mismatch(self) -> None:
        self.assertFalse(
            Prefix("10.10.0.0/16").contains(Prefix("10.20.0.0/16"))
        )
        # 不同地址族互不包含，而不是抛异常
        self.assertFalse(
            Prefix("10.0.0.0/8").contains(Prefix("2001:db8::/32"))
        )

    def test_equality_and_hash(self) -> None:
        self.assertEqual(Prefix("10.10.1.0/24"), Prefix("10.10.1.7/24"))
        self.assertEqual(
            hash(Prefix("10.10.1.0/24")), hash(Prefix("10.10.1.7/24"))
        )
        self.assertNotEqual(Prefix("10.10.1.0/24"), Prefix("10.10.1.0/25"))

    def test_ordering(self) -> None:
        self.assertLess(Prefix("10.0.0.0/8"), Prefix("10.0.0.0/16"))
        self.assertLess(Prefix("10.0.0.0/24"), Prefix("10.0.1.0/24"))

    def test_invalid_prefix_raises(self) -> None:
        for bad in ("not-a-prefix", "10.0.0.0/99", "999.0.0.1/8", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidPrefixError):
                    Prefix(bad)

    def test_non_string_raises(self) -> None:
        with self.assertRaises(InvalidPrefixError):
            Prefix(123)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
