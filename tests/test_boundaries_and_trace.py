from __future__ import annotations

import hashlib
import json
import unittest

from forensic_reader import ForensicReader


class BoundarySegmentationAndTraceTests(unittest.TestCase):
    def test_partial_final_word_is_supported(self) -> None:
        chips = {
            "A": bytes([0x00, 0x01, 0x02]),
            "B": bytes([0x10, 0x11, 0x12]),
        }
        reader = ForensicReader(
            chips,
            [{"id": "AB", "order": ["A", "B"]}],
            word_size=2,
        )
        self.assertEqual(reader.image_for("AB").hex(), "000110110212")

    def test_length_keyword_slice_and_arbitrary_interval(self) -> None:
        chips = {"A": bytes([0, 1, 2, 3]), "B": bytes([4, 5, 6, 7])}
        reader = ForensicReader(
            chips,
            [{"id": "AB", "order": ["A", "B"]}, {"id": "BA", "order": ["B", "A"]}],
            word_size=1,
        )
        by_length = reader.read(2, length=4)
        by_end = reader.read(2, 6)
        sliced = reader[2:6]
        self.assertEqual((by_length.start, by_length.end), (2, 6))
        self.assertEqual(reader.read(address=2, size=4).end, 6)
        self.assertEqual(reader.read_at(2, 4).start, 2)
        by_length_data = by_length.to_dict()
        by_end_data = by_end.to_dict()
        by_length_data.pop("answer_id")
        by_end_data.pop("answer_id")
        self.assertEqual(by_length_data, by_end_data)
        self.assertEqual(sliced.start, 2)
        self.assertEqual(sliced.end, 6)
        self.assertEqual(len(by_length.byte_states), 4)
        # Segments exactly tile the requested interval.
        covered = [(s.start, s.end) for s in by_length.segments]
        self.assertEqual(covered[0][0], 2)
        self.assertEqual(covered[-1][1], 6)
        for left, right in zip(covered, covered[1:]):
            self.assertEqual(left[1], right[0])

    def test_ambiguous_segments_split_when_support_groups_change(self) -> None:
        chips = {name: b"\x00" * 4 for name in ("A", "B", "C")}
        wirings = [
            {"id": "W1", "order": ["A", "B", "C"]},
            {"id": "W2", "order": ["B", "C", "A"]},
            {"id": "W3", "order": ["C", "A", "B"]},
        ]
        reader = ForensicReader(chips, wirings, word_size=1)
        # Inject synthetic candidate images to force two support partitions.
        reader._image_cache.update(
            {
                "W1": bytes([0, 0, 1, 2]),
                "W2": bytes([0, 0, 3, 4]),
                "W3": bytes([9, 9, 1, 2]),
            }
        )
        report = reader.read(0, 4)
        self.assertEqual([(s.start, s.end) for s in report.segments], [(0, 2), (2, 4)])
        first, second = report.segments
        first_support = {item.data.hex(): item.wiring_ids for item in first.interpretations}
        second_support = {item.data.hex(): item.wiring_ids for item in second.interpretations}
        self.assertEqual(first_support, {"0000": ("W1", "W2"), "0909": ("W3",)})
        self.assertEqual(second_support, {"0102": ("W1", "W3"), "0304": ("W2",)})
        for segment in report.segments:
            self.assertGreaterEqual(len(segment.interpretations), 2)

    def test_constructor_evidence_and_page_digest_list(self) -> None:
        logical = bytes(range(12))
        # word_size=1, order A,B means even/odd logical bytes.
        chips = {"A": logical[0::2], "B": logical[1::2]}
        reader = ForensicReader(
            chips,
            [{"id": "AB", "order": ["A", "B"]}, {"id": "BA", "order": ["B", "A"]}],
            word_size=1,
            known_bytes=[{"address": 0, "value": 0, "source": "reset-vector"}],
            page_size=4,
            page_digests=[hashlib.sha256(logical[0:4]).hexdigest()],
        )
        # known byte leaves AB; page 0 digest also agrees with AB.
        self.assertEqual(reader.version, 2)
        self.assertEqual(reader.surviving_wiring_ids, ("AB",))
        self.assertEqual(reader.read_bytes(0, 12), logical)

    def test_multiple_evidence_versions_trace_each_prior_judgment(self) -> None:
        chips = {"A": bytes([0, 0, 2, 3]), "B": bytes([0, 5, 6, 7])}
        wirings = [
            {"id": "AB", "order": ["A", "B"]},
            {"id": "BA", "order": ["B", "A"]},
        ]
        reader = ForensicReader(chips, wirings, word_size=1)

        # Addresses 0 and 1 are the same repeated zero under both orders.
        # Address 2 is A[1] versus B[1], so it distinguishes the candidates.
        initial = reader.read(0, 8)
        self.assertEqual(initial.byte_at(0).status, "confirmed")
        self.assertEqual(initial.byte_at(1).status, "confirmed")
        self.assertEqual(initial.byte_at(2).status, "ambiguous")

        reader.add_known_byte(4, 2, source="checksum-table")  # AB A[2]=2; BA B[2]=6
        after_first = reader.read(0, 8)
        self.assertEqual(reader.surviving_wiring_ids, ("AB",))
        self.assertTrue(after_first.fully_confirmed)

        reader.add_known_byte(6, 3, source="last-byte")
        trace = reader.trace(0, 8)
        reconstructed_initial = reader.report_at_version(0, 0, 8)
        self.assertEqual(reconstructed_initial.state, "ambiguous")
        self.assertEqual([entry.version for entry in trace.entries], [0, 1, 2])
        self.assertEqual(trace.entries[1].report.version, 1)
        self.assertEqual(trace.entries[1].evidence_summary, "known byte 0x4=0x02 from checksum-table")
        self.assertTrue(trace.entries[1].report.fully_confirmed)
        self.assertEqual(reader.version, 2)
        self.assertEqual(len(reader.history[0].rejections), 1)
        self.assertEqual(reader.history[1].rejections, ())
        json.dumps(trace.to_dict())

    def test_bare_order_and_list_of_orders(self) -> None:
        chips = {"A": b"\x00\x01", "B": b"\x02\x03"}
        bare = ForensicReader(chips, ("A", "B"), word_size=1)
        self.assertEqual(bare.wiring_ids, ("W1",))
        listed = ForensicReader(chips, [("A", "B"), ("B", "A")], word_size=1)
        self.assertEqual(listed.candidate_count, 2)
        self.assertEqual(listed.image_for("W1"), b"\x00\x02\x01\x03")


if __name__ == "__main__":
    unittest.main()
