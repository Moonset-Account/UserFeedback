from __future__ import annotations

import hashlib
import json
import unittest
import zlib

from forensic_reader import (
    AmbiguousReadError,
    EvidenceFormatError,
    ForensicReader,
    LogicalRangeError,
)
from _synthetic import split_logical_image


class ConsensusAndEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.chips = {
            "A": bytes([0x00, 0x01, 0x02, 0x03]),
            "B": bytes([0x10, 0x11, 0x12, 0x13]),
        }
        self.wirings = [
            {"id": "AB", "order": ["A", "B"]},
            {"id": "BA", "order": ["B", "A"]},
        ]

    def test_ambiguous_interval_lists_at_least_two_interpretations(self) -> None:
        reader = ForensicReader(self.chips, self.wirings, word_size=2)
        report = reader.read(0, 8)
        self.assertEqual(report.state, "ambiguous")
        self.assertFalse(report.fully_confirmed)
        self.assertEqual(len(report.ambiguous_segments), 1)
        segment = report.segments[0]
        self.assertEqual(segment.start, 0)
        self.assertEqual(segment.end, 8)
        self.assertGreaterEqual(len(segment.interpretations), 2)
        interpreted = {item.data.hex(): item.wiring_ids for item in segment.interpretations}
        self.assertEqual(
            interpreted,
            {
                "0001101102031213": ("AB",),
                "1011000112130203": ("BA",),
            },
        )
        # Every ambiguous byte exposes distinct possible values.
        self.assertGreaterEqual(len(report.byte_at(0).possible_values), 2)
        json.dumps(report.to_dict())

    def test_known_byte_filters_candidate_and_confirms_range(self) -> None:
        reader = ForensicReader(self.chips, self.wirings, word_size=2)
        entry = reader.add_known_byte(0, 0x00, source="logic-analyzer-1")
        self.assertEqual(entry.removed_ids, ("BA",))
        self.assertEqual(reader.surviving_wiring_ids, ("AB",))
        rejection = entry.rejections[0]
        self.assertEqual(rejection.wiring_id, "BA")
        self.assertEqual(rejection.reason, "known_byte_mismatch")
        self.assertEqual(rejection.expected, 0x00)
        self.assertEqual(rejection.actual, 0x10)
        self.assertEqual(rejection.details["source"], "logic-analyzer-1")

        report = reader.read(0, 8)
        self.assertTrue(report.fully_confirmed)
        self.assertEqual(bytes(report).hex(), "0001101102031213")

    def test_page_sha256_filters_candidate(self) -> None:
        reader = ForensicReader(self.chips, self.wirings, word_size=2)
        expected = hashlib.sha256(bytes.fromhex("0001101102031213")).hexdigest()
        entry = reader.add_page_checksum(
            page=0,
            page_size=8,
            algorithm="sha256",
            digest=expected,
            source="page-label",
        )
        self.assertEqual(entry.after_ids, ("AB",))
        self.assertEqual(entry.removed_ids, ("BA",))
        report = reader.read(0, 8)
        self.assertTrue(report.fully_confirmed)

    def test_crc32_page_digest_is_supported(self) -> None:
        reader = ForensicReader(self.chips, self.wirings, word_size=2)
        data = bytes.fromhex("0001101102031213")
        digest = f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
        reader.add_page_checksum(start=0, end=8, algorithm="crc32", digest=digest)
        self.assertEqual(reader.surviving_wiring_ids, ("AB",))

    def test_simple_xor_and_sum_digests_are_supported(self) -> None:
        data = bytes.fromhex("0001101102031213")
        xor_value = 0
        for byte in data:
            xor_value ^= byte
        reader = ForensicReader(self.chips, self.wirings, word_size=2)
        reader.add_page_checksum(
            start=0,
            end=8,
            algorithm="xor8",
            digest=f"{xor_value:02x}",
        )
        # XOR and additive sums are order-insensitive. They are accepted as
        # evidence, but correctly cannot distinguish the two reordered images.
        self.assertEqual(reader.surviving_wiring_ids, ("AB", "BA"))

        sum_reader = ForensicReader(self.chips, self.wirings, word_size=2)
        entry = sum_reader.add_page_checksum(
            start=0,
            end=8,
            algorithm="sum16",
            digest=f"{sum(data) & 0xFFFF:04x}",
        )
        self.assertEqual(entry.after_ids, ("AB", "BA"))
        self.assertEqual(entry.rejections, ())

    def test_multiple_pages_with_explicit_page_lengths(self) -> None:
        logical = bytes((index * 7 + 3) & 0xFF for index in range(16))
        chips = split_logical_image(logical, ("A", "B"), word_size=2)
        reader = ForensicReader(
            chips,
            [{"id": "AB", "order": ["A", "B"]}, {"id": "BA", "order": ["B", "A"]}],
            word_size=2,
        )
        digest0 = hashlib.sha256(logical[0:8]).hexdigest()
        digest1 = hashlib.sha256(logical[8:16]).hexdigest()
        reader.add_page_checksum(page=0, page_size=8, digest=digest0, source="page-0")
        reader.add_page_checksum(page=1, page_size=8, digest=digest1, source="page-1")
        self.assertEqual(reader.surviving_wiring_ids, ("AB",))
        self.assertEqual(reader.read_bytes(0, 16), logical)

    def test_repeated_bytes_keep_page_confirmed_under_multiple_wirings(self) -> None:
        repeated = bytes([0x5A] * 4)
        chips = {
            "A": repeated + bytes([0x01, 0x02, 0x03, 0x04]),
            "B": repeated + bytes([0x11, 0x12, 0x13, 0x14]),
        }
        reader = ForensicReader(chips, self.wirings, word_size=1)
        page_digest = hashlib.sha256(b"\x5a" * 8).hexdigest()
        entry = reader.add_page_checksum(page=0, page_size=8, digest=page_digest)
        # The page digest cannot distinguish the two orders because the repeated
        # prefix makes their first eight logical bytes identical.
        self.assertEqual(entry.after_ids, ("AB", "BA"))
        self.assertEqual(reader.surviving_wiring_ids, ("AB", "BA"))

        page = reader.read(0, 8)
        self.assertTrue(page.fully_confirmed)
        self.assertTrue(page.has_multiple_wirings)
        self.assertEqual(page.survivor_count, 2)
        self.assertEqual(page.state, "confirmed")
        self.assertEqual(bytes(page), b"\x5a" * 8)
        self.assertEqual(page.segments[0].wiring_ids, ("AB", "BA"))
        self.assertGreaterEqual(page.segments[0].wiring_count, 2)

        whole = reader.read(0, 16)
        self.assertEqual(whole.confirmed_count, 8)
        self.assertEqual(whole.ambiguous_count, 8)
        self.assertEqual(len(whole.ambiguous_segments), 1)

    def test_adding_evidence_reanswers_and_preserves_original_judgment(self) -> None:
        reader = ForensicReader(self.chips, self.wirings, word_size=2)
        original_answer = reader.read(0, 8)
        self.assertEqual(original_answer.version, 0)
        self.assertEqual(original_answer.answer_id, 1)

        reader.add_known_byte(0, 0x00, source="rom-silkscreen")
        new_answer = reader.read(0, 8)
        self.assertEqual(new_answer.version, 1)
        self.assertTrue(new_answer.fully_confirmed)

        logged = reader.answer_history_for(0, 8)
        self.assertEqual([report.answer_id for report in logged], [1, 2])
        self.assertEqual(logged[0].state, "ambiguous")
        self.assertEqual(logged[1].state, "confirmed")

        trace = reader.trace(0, 8)
        self.assertEqual(len(trace), 2)
        branched = reader.with_evidence(
            {"address": 7, "value": 0x13, "source": "branch-evidence"}
        )
        self.assertEqual(reader.version, 1)
        self.assertEqual(branched.version, 2)
        self.assertEqual(branched.surviving_wiring_ids, ("AB",))
        self.assertEqual(trace.initial.state, "ambiguous")
        self.assertGreaterEqual(len(trace.initial.segments[0].interpretations), 2)
        self.assertEqual(trace.current.state, "confirmed")
        diff = trace.entries[-1].diff_from_previous
        self.assertEqual(diff.removed_wiring_ids, ("BA",))
        self.assertEqual(len(diff.newly_confirmed), 1)
        self.assertEqual(diff.newly_confirmed[0].start, 0)
        self.assertEqual(diff.newly_confirmed[0].end, 8)
        self.assertEqual(diff.newly_confirmed[0].data.hex(), "0001101102031213")
        json.dumps(trace.to_dict())

    def test_three_candidates_can_leave_partial_ambiguity_after_evidence(self) -> None:
        wirings = [
            {"id": "AB", "order": ["A", "B"]},
            {"id": "BA", "order": ["B", "A"]},
            {"id": "AB-word", "order": ["A", "B"], "reversal": {"A": "word"}},
        ]
        reader = ForensicReader(self.chips, wirings, word_size=2)
        reader.add_known_byte(0, 0x00)  # keeps AB only because AB-word starts 0x01
        report = reader.read(0, 8)
        self.assertEqual(reader.surviving_wiring_ids, ("AB",))
        self.assertTrue(report.fully_confirmed)

    def test_all_candidates_rejected_is_explicit_not_confirmed(self) -> None:
        reader = ForensicReader(self.chips, self.wirings, word_size=2)
        entry = reader.add_known_byte(0, 0xFF, source="contradictory-note")
        self.assertEqual(entry.after_ids, ())
        self.assertFalse(reader.consistent)
        report = reader.read(0, 8)
        self.assertEqual(report.state, "no_consistent_wiring")
        self.assertFalse(report.fully_confirmed)
        self.assertEqual(report.segments[0].status, "no_consistent_wiring")
        with self.assertRaises(AmbiguousReadError):
            report.confirmed_bytes()
        with self.assertRaises(AmbiguousReadError):
            reader.read_bytes(0, 8)

        trace = reader.trace(0, 8)
        self.assertEqual(trace.initial.state, "ambiguous")
        self.assertEqual(trace.current.state, "no_consistent_wiring")
        self.assertTrue(trace.entries[-1].diff_from_previous.lost_ranges)

    def test_invalid_evidence_and_ranges_do_not_mutate_history(self) -> None:
        reader = ForensicReader(self.chips, self.wirings, word_size=2)
        with self.assertRaises(EvidenceFormatError):
            reader.add_known_byte(99, 0x00)
        with self.assertRaises(EvidenceFormatError):
            reader.add_page_checksum(page=0, page_size=8, digest="not-hex")
        self.assertEqual(reader.version, 0)
        with self.assertRaises(LogicalRangeError):
            reader.read(-1, 2)
        with self.assertRaises(LogicalRangeError):
            reader.read(0, 99)
        with self.assertRaises(LogicalRangeError):
            reader.read(2, 1)

    def test_empty_interval_is_confirmed_without_bytes(self) -> None:
        reader = ForensicReader(self.chips, self.wirings, word_size=2)
        report = reader.read(4, 4)
        self.assertTrue(report.fully_confirmed)
        self.assertEqual(report.confirmed_bytes(), b"")
        self.assertEqual(report.segments, ())


if __name__ == "__main__":
    unittest.main()
