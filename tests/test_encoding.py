from __future__ import annotations

import math
import unittest

from blind_telemetry.encoding import (
    decode_credential_message,
    encode_credential_message,
    hash_to_unit,
    length_prefix,
    read_length_prefixed,
)
from blind_telemetry.errors import EncodingError
from blind_telemetry.rsa import DEMO_N


class LengthPrefixTests(unittest.TestCase):
    def test_length_prefix_uses_big_endian_uint32(self) -> None:
        self.assertEqual(length_prefix(b"abc"), (3).to_bytes(4, "big") + b"abc")
        value, offset = read_length_prefixed(b"\x00\x00\x00\x03abc")
        self.assertEqual(value, b"abc")
        self.assertEqual(offset, 7)

    def test_encoding_is_strictly_roundtrippable(self) -> None:
        blob = encode_credential_message(20261126, b"ticket-bytes")
        period_raw, ticket_raw = decode_credential_message(blob)
        self.assertEqual(period_raw, b"20261126")
        self.assertEqual(ticket_raw, b"ticket-bytes")

    def test_length_prefixes_remove_concatenation_ambiguity(self) -> None:
        first = encode_credential_message(1, b"ab")
        # Compare repartitionable raw fields; their framed forms differ.
        framed_a = length_prefix(b"1a") + length_prefix(b"b")
        framed_b = length_prefix(b"1") + length_prefix(b"ab")
        self.assertNotEqual(framed_a, framed_b)
        self.assertNotEqual(first, encode_credential_message(1, b"ab-other"))

    def test_truncated_and_trailing_data_are_rejected(self) -> None:
        good = encode_credential_message(1, b"ticket")
        with self.assertRaises(EncodingError):
            decode_credential_message(good[:3])
        with self.assertRaises(EncodingError):
            decode_credential_message(good[:-1])
        with self.assertRaises(EncodingError):
            decode_credential_message(good + b"trailing")

    def test_empty_ticket_and_bad_period_are_rejected(self) -> None:
        with self.assertRaises(EncodingError):
            encode_credential_message(1, b"")
        with self.assertRaises((TypeError, EncodingError)):
            encode_credential_message(-1, b"ticket")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            encode_credential_message(True, b"ticket")  # type: ignore[arg-type]


class HashToUnitTests(unittest.TestCase):
    def test_result_is_deterministic_and_invertible(self) -> None:
        first = hash_to_unit(DEMO_N, period=7, ticket=b"fixed-ticket-0001")
        second = hash_to_unit(DEMO_N, period=7, ticket=b"fixed-ticket-0001")
        self.assertEqual(first, second)
        self.assertGreaterEqual(first.counter, 0)
        self.assertTrue(2 <= first.value < DEMO_N)
        self.assertEqual(math.gcd(first.value, DEMO_N), 1)

    def test_period_or_ticket_changes_hash(self) -> None:
        base = hash_to_unit(DEMO_N, period=1, ticket=b"fixed-ticket-0001")
        other_period = hash_to_unit(DEMO_N, period=2, ticket=b"fixed-ticket-0001")
        other_ticket = hash_to_unit(DEMO_N, period=1, ticket=b"fixed-ticket-0002")
        self.assertNotEqual(base.value, other_period.value)
        self.assertNotEqual(base.value, other_ticket.value)

    def test_encoded_argument_matches_fields(self) -> None:
        encoded = encode_credential_message(3, b"another-ticket")
        via_encoded = hash_to_unit(DEMO_N, encoded_message=encoded)
        via_fields = hash_to_unit(DEMO_N, period=3, ticket=b"another-ticket")
        self.assertEqual(via_encoded, via_fields)


if __name__ == "__main__":
    unittest.main()
