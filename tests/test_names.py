"""Unit tests for domain-name parsing and canonical rendering."""

from __future__ import annotations

import unittest

from authoritative_dns.names import (
    InvalidDNSName,
    is_subdomain,
    parse_name,
    proper_ancestors,
    to_text,
    wildcard_child,
)


class ParseNameTests(unittest.TestCase):
    def test_absolute_and_relative_forms_match(self):
        self.assertEqual(parse_name("WWW.Example."), ("www", "example"))
        self.assertEqual(parse_name("www.example"), ("www", "example"))
        self.assertEqual(parse_name("  www.example.  "), ("www", "example"))

    def test_root_name(self):
        self.assertEqual(parse_name("."), ())
        self.assertEqual(to_text(()), ".")

    def test_round_trip(self):
        for text in ("example.", "a.b.example.", "*.example."):
            self.assertEqual(to_text(parse_name(text)), text)

    def test_wildcard_label_must_stand_alone(self):
        self.assertEqual(parse_name("*.example."), ("*", "example"))
        with self.assertRaises(InvalidDNSName):
            parse_name("w*.example.")

    def test_invalid_names_are_refused(self):
        for bad in ("", "   ", "a..example", "exa mple.", "bad!label.example."):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidDNSName):
                    parse_name(bad)
        with self.assertRaises(TypeError):
            parse_name(b"example.")

    def test_label_and_length_limits(self):
        with self.assertRaises(InvalidDNSName):
            parse_name("a" * 64 + ".example.")
        deep = ".".join(["a" * 60] * 5) + ".example."
        with self.assertRaises(InvalidDNSName):
            parse_name(deep)


class NameRelationTests(unittest.TestCase):
    def test_subdomain_is_reflexive(self):
        self.assertTrue(is_subdomain(("www", "example"), ("www", "example")))
        self.assertTrue(is_subdomain(("x", "www", "example"), ("www", "example")))
        self.assertFalse(is_subdomain(("example",), ("www", "example")))
        self.assertTrue(is_subdomain(("example",), ()))

    def test_proper_ancestors_go_upwards(self):
        self.assertEqual(
            list(proper_ancestors(("y", "b", "example"))),
            [("b", "example"), ("example",), ()],
        )

    def test_wildcard_child(self):
        self.assertEqual(wildcard_child(("example",)), ("*", "example"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
