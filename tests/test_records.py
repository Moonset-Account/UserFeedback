"""Unit tests for resource-record construction and validation."""

from __future__ import annotations

import unittest

from authoritative_dns.records import InvalidRecord, Record


class RecordValidationTests(unittest.TestCase):
    def test_a_and_aaaa_are_canonicalised(self):
        record = Record("WWW.Example.", "a", "192.0.2.10")
        self.assertEqual(record.name, "www.example.")
        self.assertEqual(record.rtype, "A")
        self.assertEqual(record.value, "192.0.2.10")

        v6 = Record("www.example.", "AAAA", "2001:DB8:0:0:0:0:0:10")
        self.assertEqual(v6.value, "2001:db8::10")

    def test_txt_single_and_multiple_character_strings(self):
        self.assertEqual(Record("a.example.", "TXT", "solo").value, ("solo",))
        multi = Record("a.example.", "TXT", ("hello", "world"))
        self.assertEqual(multi.value, ("hello", "world"))
        self.assertEqual(
            Record("a.example.", "TXT", ["x", "y"]).value, ("x", "y")
        )

    def test_ns_target_is_canonicalised(self):
        record = Record("child.example.", "NS", "NS1.Child.Example.")
        self.assertEqual(record.value, "ns1.child.example.")

    def test_invalid_addresses_are_rejected(self):
        for rtype, value in (
            ("A", "192.0.2.256"),
            ("A", "not-an-ip"),
            ("A", "2001:db8::1"),
            ("AAAA", "192.0.2.1"),
            ("AAAA", "2001:db8::zz"),
        ):
            with self.subTest(rtype=rtype, value=value):
                with self.assertRaises(InvalidRecord):
                    Record("a.example.", rtype, value)

    def test_unsupported_and_malformed_types_are_rejected(self):
        with self.assertRaises(InvalidRecord):
            Record("a.example.", "MX", "10 mail.example.")
        with self.assertRaises(InvalidRecord):
            Record("a.example.", "CNAME", "target.example.")
        with self.assertRaises(InvalidRecord):
            Record("a.example.", "", "x")
        with self.assertRaises(InvalidRecord):
            Record("a.example.", "A B", "192.0.2.1")

    def test_txt_chunk_length_limit(self):
        with self.assertRaises(InvalidRecord):
            Record("a.example.", "TXT", "x" * 256)
        self.assertEqual(len(Record("a.example.", "TXT", "x" * 255).value[0]), 255)

    def test_bad_owner_names_and_ttls(self):
        with self.assertRaises(InvalidRecord):
            Record("bad..name.", "A", "192.0.2.1")
        with self.assertRaises(InvalidRecord):
            Record("a.example.", "A", "192.0.2.1", ttl=-1)
        with self.assertRaises(InvalidRecord):
            Record("a.example.", "A", "192.0.2.1", ttl=True)

    def test_equality_ignores_input_case_and_trailing_dot(self):
        first = Record("WWW.example", "A", "192.0.2.10", ttl=60)
        second = Record("www.example.", "a", "192.0.2.10", ttl=60)
        self.assertEqual(first, second)

    def test_to_dict_renders_txt_as_list(self):
        payload = Record("a.example.", "TXT", ("one", "two")).to_dict()
        self.assertEqual(
            payload,
            {"name": "a.example.", "type": "TXT", "ttl": 300,
             "value": ["one", "two"]},
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
