from fractions import Fraction
from itertools import permutations
import unittest

from memory_accounting import GapError, ReplaySession, replay_receipts


PAGE = 4096


def resident_event(position, source, pid, pfn, reuse, **kwargs):
    return {
        "position": position,
        "source": source,
        "kind": "resident",
        "pid": pid,
        "pfn": pfn,
        "reuse_id": reuse,
        **kwargs,
    }


class CompletenessAndReplayTests(unittest.TestCase):
    def test_complete_snapshot_position(self) -> None:
        result = replay_receipts(
            {
                "page_size": PAGE,
                "receipts": [
                    {
                        "position": 1,
                        "source": "collector",
                        "processes": [
                            {
                                "pid": "A",
                                "mappings": [
                                    {
                                        "mapping_id": "a1",
                                        "start": 0,
                                        "end": PAGE,
                                    }
                                ],
                                "pages": [
                                    {
                                        "mapping_id": "a1",
                                        "vaddr": 0,
                                        "pfn": 10,
                                        "reuse_id": 1,
                                    }
                                ],
                            },
                            {
                                "pid": "B",
                                "mappings": [
                                    {
                                        "mapping_id": "b1",
                                        "start": 0,
                                        "end": PAGE,
                                    }
                                ],
                                "pages": [
                                    {
                                        "mapping_id": "b1",
                                        "vaddr": 0,
                                        "pfn": 10,
                                        "reuse_id": 1,
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        )

        self.assertEqual(result.watermark, 1)
        self.assertEqual(result.latest_accountable_position, 1)
        self.assertEqual(result.gaps, ())
        self.assertEqual(result.report.physical_frame_count, 1)
        for process in result.report.processes.values():
            self.assertEqual(process.proportional_bytes, PAGE // 2)

    def test_missing_position_gives_watermark_and_gap(self) -> None:
        session = ReplaySession(page_size=PAGE)
        session.ingest(resident_event(1, "c", "A", 1, 1))
        session.ingest(resident_event(3, "c", "A", 3, 1))

        result = session.result()
        self.assertEqual(result.watermark, 1)
        self.assertEqual(result.latest_accountable_position, 1)
        self.assertEqual(result.latest_complete_position, 3)
        self.assertEqual([g.position for g in result.gaps], [2])
        self.assertEqual(result.gaps[0].state, "missing_position")
        self.assertEqual(result.report.physical_frame_count, 1)

        # Filling the gap advances the accountable watermark.
        session.ingest(resident_event(2, "c", "A", 2, 1))
        final = session.result()
        self.assertEqual(final.watermark, 3)
        self.assertEqual([g.position for g in final.gaps], [])
        self.assertEqual(final.report.physical_frame_count, 3)
        self.assertEqual(set(final.reports), {1, 2, 3})

    def test_expected_sources_missing_source_is_gap(self) -> None:
        session = ReplaySession(
            page_size=PAGE, expected_sources=["collector-a", "collector-b"]
        )
        session.ingest(resident_event(1, "collector-a", "A", 1, 1))
        session.ingest(resident_event(2, "collector-a", "A", 2, 1))
        session.ingest(resident_event(3, "collector-a", "A", 3, 1))
        session.ingest(resident_event(3, "collector-b", "B", 3, 1))

        result = session.result()
        self.assertEqual(result.watermark, None)
        self.assertEqual(result.latest_complete_position, 3)
        self.assertEqual([g.position for g in result.gaps], [1, 2])
        self.assertEqual(result.gaps[0].state, "incomplete")
        self.assertEqual(
            result.gaps[0].missing_sources, ("collector-b",)
        )

        session.ingest(resident_event(1, "collector-b", "B", 1, 1))
        session.ingest(resident_event(2, "collector-b", "B", 2, 1))
        final = session.result()
        self.assertEqual(final.watermark, 3)
        self.assertEqual(final.gaps, ())
        # A and B observe the same PFN at each position, so each position adds
        # one shared physical frame.
        self.assertEqual(final.report.physical_frame_count, 3)
        self.assertEqual(
            {len(f.holders) for f in final.report.frames}, {2}
        )

    def test_per_position_expected_sources_and_expected_positions(self) -> None:
        session = ReplaySession(
            page_size=PAGE,
            expected_sources={1: ["a"], 2: ["a", "b"]},
            expected_positions=[1, 2, 3, 4],
        )
        session.ingest(resident_event(1, "a", "A", 1, 1))
        session.ingest(resident_event(2, "a", "A", 2, 1))
        session.ingest(resident_event(2, "b", "B", 2, 1))

        result = session.result()
        self.assertEqual(result.watermark, 2)
        self.assertEqual([g.position for g in result.gaps], [3, 4])
        self.assertTrue(all(g.state == "missing_position" for g in result.gaps))

    def test_require_seal(self) -> None:
        session = ReplaySession(page_size=PAGE, require_seal=True)
        session.ingest(resident_event(1, "c", "A", 1, 1))
        self.assertIsNone(session.result().watermark)

        session.seal_position(1)
        result = session.result()
        self.assertEqual(result.watermark, 1)

    def test_confirm_position_rejects_missing_expected_source(self) -> None:
        session = ReplaySession(page_size=PAGE, expected_sources=["a", "b"])
        session.ingest(resident_event(1, "a", "A", 1, 1))
        with self.assertRaises(GapError):
            session.confirm_position(1)

        # An authoritative completion marker can still seal an empty position.
        session.mark_complete(2)
        self.assertTrue(session.status(2).complete)

    def test_out_of_order_replay_and_late_old_generation_revoke(self) -> None:
        receipts = [
            {
                "position": 3,
                "source": "c",
                "kind": "revoke",
                "pfn": 5,
                "reuse_id": 1,
            },
            resident_event(1, "c", "A", 5, 1),
            resident_event(2, "c", "B", 5, 2),
        ]
        result = replay_receipts({"page_size": PAGE, "receipts": receipts})

        self.assertEqual(result.watermark, 3)
        self.assertEqual(result.report.physical_frame_count, 1)
        frame = result.report.frames[0]
        self.assertEqual(frame.frame.number, 5)
        self.assertEqual(frame.frame.reuse_id, 2)
        self.assertEqual(frame.holders, ("B",))

        # Reports at each logical position show the temporary overlap at pos 2
        # before the late old-generation tombstone is replayed at pos 3.
        self.assertEqual(result.reports[1].physical_frame_count, 1)
        self.assertEqual(result.reports[2].physical_frame_count, 2)
        self.assertEqual(result.reports[3].physical_frame_count, 1)

    def test_every_arrival_permutation_replays_to_same_generation_result(self) -> None:
        receipts = [
            resident_event(1, "c", "A", 5, 1),
            resident_event(2, "c", "B", 5, 2),
            {"position": 3, "source": "c", "kind": "revoke",
             "pfn": 5, "reuse_id": 1},
        ]

        for order in permutations(range(len(receipts))):
            session = ReplaySession(page_size=PAGE)
            for index in order:
                session.ingest(receipts[index])
            with self.subTest(order=order):
                result = session.result()
                self.assertEqual(result.watermark, 3)
                self.assertEqual(result.report.physical_frame_count, 1)
                self.assertEqual(result.report.frames[0].frame.reuse_id, 2)
                self.assertEqual(result.report.frames[0].holders, ("B",))

    def test_late_duplicate_revoke_after_completion_cannot_remove_new_frame(self) -> None:
        session = ReplaySession(page_size=PAGE)
        session.ingest(resident_event(1, "c", "A", 5, 1))
        session.ingest(
            {"position": 2, "source": "c", "kind": "revoke", "pfn": 5, "reuse_id": 1}
        )
        session.ingest(resident_event(3, "c", "B", 5, 2))
        first = session.result()
        self.assertEqual(first.watermark, 3)
        self.assertEqual(first.report.physical_frame_count, 1)

        # Late redelivery of the old tombstone is accepted for audit/replay but
        # its composite generation key is absent.
        session.ingest(
            {"position": 3, "source": "late", "kind": "revoke", "pfn": 5, "reuse_id": 1}
        )
        final = session.result()
        self.assertEqual(final.report.physical_frame_count, 1)
        self.assertEqual(final.report.frames[0].frame.reuse_id, 2)
        self.assertGreaterEqual(final.stale_removal_count, 1)

    def test_duplicate_receipt_id_is_idempotent(self) -> None:
        session = ReplaySession(page_size=PAGE)
        receipt = {
            "receipt_id": "r1",
            "position": 1,
            "source": "c",
            **resident_event(1, "c", "A", 9, 1),
        }
        session.ingest(receipt)
        session.ingest(dict(receipt))
        result = session.result()
        self.assertEqual(result.report.physical_frame_count, 1)
        self.assertEqual(result.duplicate_receipt_ids, ("r1",))

    def test_cow_event_replay(self) -> None:
        result = replay_receipts(
            {
                "page_size": PAGE,
                "receipts": [
                    {
                        "position": 1,
                        "source": "c",
                        "events": [
                            {"kind": "mapping", "pid": "A", "mapping_id": "m",
                             "start": 0, "end": PAGE},
                            {"kind": "mapping", "pid": "B", "mapping_id": "m",
                             "start": 0, "end": PAGE},
                            {"kind": "resident", "pid": "A", "mapping_id": "m",
                             "vaddr": 0, "pfn": 7, "reuse_id": 1},
                            {"kind": "resident", "pid": "B", "mapping_id": "m",
                             "vaddr": 0, "pfn": 7, "reuse_id": 1},
                        ],
                    },
                    {
                        "position": 2,
                        "source": "c",
                        "events": [
                            {
                                "kind": "cow",
                                "pid": "A",
                                "mapping_id": "m",
                                "vaddr": 0,
                                "old_pfn": 7,
                                "old_reuse_id": 1,
                                "new_pfn": 8,
                                "new_reuse_id": 1,
                            }
                        ],
                    },
                ],
            }
        )
        self.assertEqual(result.watermark, 2)
        report = result.report
        self.assertEqual(report.physical_frame_count, 2)
        self.assertEqual(report.processes["A"].exclusive_bytes, PAGE)
        self.assertEqual(report.processes["B"].exclusive_bytes, PAGE)

    def test_snapshot_reconciles_removed_pages(self) -> None:
        session = ReplaySession(page_size=PAGE)
        session.ingest(
            {
                "position": 1,
                "source": "c",
                "pid": "A",
                "mappings": [{"mapping_id": "a", "start": 0, "end": 2 * PAGE}],
                "pages": [
                    {"mapping_id": "a", "vaddr": 0, "pfn": 1, "reuse_id": 1},
                    {"mapping_id": "a", "vaddr": PAGE, "pfn": 2, "reuse_id": 1},
                ],
            }
        )
        self.assertEqual(session.result().report.physical_frame_count, 2)

        session.ingest(
            {
                "position": 2,
                "source": "c",
                "pid": "A",
                "mappings": [{"mapping_id": "a", "start": 0, "end": 2 * PAGE}],
                "pages": [
                    {"mapping_id": "a", "vaddr": 0, "pfn": 1, "reuse_id": 1},
                ],
            }
        )
        result = session.result()
        self.assertEqual(result.watermark, 2)
        self.assertEqual(result.report.physical_frame_count, 1)

    def test_isolated_report_for_complete_position_after_gap(self) -> None:
        session = ReplaySession(page_size=PAGE)
        session.ingest(
            {
                "position": 1,
                "source": "c",
                "pid": "A",
                "pages": [{"pfn": 1, "reuse_id": 1}],
            }
        )
        # Position 2 is absent.  Position 3 is nevertheless a complete snapshot.
        session.ingest(
            {
                "position": 3,
                "source": "c",
                "processes": [
                    {"pid": "A", "pages": [{"pfn": 8, "reuse_id": 2}]},
                    {"pid": "B", "pages": [{"pfn": 8, "reuse_id": 2}]},
                ],
            }
        )

        result = session.result()
        self.assertEqual(result.latest_accountable_position, 1)
        self.assertEqual(result.latest_complete_position, 3)
        self.assertEqual([g.position for g in result.gaps], [2])

        isolated = result.isolated_reports[3]
        self.assertEqual(isolated.physical_frame_count, 1)
        self.assertEqual(len(isolated.processes), 2)
        self.assertEqual(
            isolated.processes["A"].proportional_pages, Fraction(1, 2)
        )
        self.assertIs(result.latest_isolated_report, isolated)

    def test_single_receipt_object_can_be_passed_directly(self) -> None:
        result = replay_receipts(
            {
                "position": 7,
                "source": "c",
                "pid": "A",
                "pages": [{"pfn": 11, "reuse_id": 2}],
            },
            first_position=7,
        )
        self.assertEqual(result.watermark, 7)
        self.assertEqual(result.report.physical_frame_count, 1)

    def test_empty_session_has_no_accountable_position(self) -> None:
        result = ReplaySession().result()
        self.assertIsNone(result.watermark)
        self.assertIsNone(result.latest_accountable_position)
        self.assertIsNone(result.latest_complete_position)
        self.assertEqual(result.gaps, ())
        self.assertEqual(result.report.physical_frame_count, 0)

    def test_json_report_contains_exact_fractions(self) -> None:
        result = replay_receipts(
            [
                {
                    "position": 1,
                    "source": "c",
                    "processes": [
                        {"pid": "A", "pages": [{"pfn": 1, "reuse_id": 1}]},
                        {"pid": "B", "pages": [{"pfn": 1, "reuse_id": 1}]},
                    ],
                }
            ]
        )
        data = result.to_dict()
        process_a = data["report"]["processes"]["A"]
        self.assertEqual(process_a["proportional_pages"]["exact"], "1/2")
        # 4096 / 2 reduces to the exact integer Fraction(2048, 1).
        self.assertEqual(process_a["proportional_bytes"]["numerator"], PAGE // 2)
        self.assertEqual(process_a["proportional_bytes"]["denominator"], 1)


if __name__ == "__main__":
    unittest.main()
