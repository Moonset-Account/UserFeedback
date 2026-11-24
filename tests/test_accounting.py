from fractions import Fraction
import unittest

from memory_accounting import FrameRef, MappingError, MemoryAccount, account_observations


PAGE = 4096


def add_standard_topology(account: MemoryAccount) -> None:
    account.declare_mapping("A", "a1", start=0x10000, end=0x12000)
    account.declare_mapping("A", "a2", start=0x20000, end=0x21000)
    account.declare_mapping("B", "b1", start=0x30000, end=0x32000)

    # A maps frame 10/1 in two different mappings, plus shared frame 20/1.
    account.add_resident(
        "A", pfn=10, reuse_id=1, mapping_id="a1", vaddr=0x10000
    )
    account.add_resident(
        "A", pfn=20, reuse_id=1, mapping_id="a1", vaddr=0x11000
    )
    account.add_resident(
        "A", pfn=10, reuse_id=1, mapping_id="a2", vaddr=0x20000
    )

    # B shares 20/1 and exclusively owns 30/1.
    account.add_resident(
        "B", pfn=20, reuse_id=1, mapping_id="b1", vaddr=0x30000
    )
    account.add_resident(
        "B", pfn=30, reuse_id=1, mapping_id="b1", vaddr=0x31000
    )


class PhysicalAccountingTests(unittest.TestCase):
    def test_unique_frames_pss_exclusive_and_duplicate_holder(self) -> None:
        account = MemoryAccount(page_size=PAGE)
        add_standard_topology(account)
        report = account.report()

        self.assertEqual(report.physical_frame_count, 3)
        self.assertEqual(report.physical_resident_bytes, 3 * PAGE)

        a = report.processes["A"]
        b = report.processes["B"]

        # Distinct resident frames, not the three mapped occurrences in A.
        self.assertEqual(a.resident_frame_count, 2)
        self.assertEqual(a.resident_bytes, 2 * PAGE)
        self.assertEqual(a.mapping_occurrence_count, 3)
        self.assertEqual(a.mapping_occurrence_bytes, 3 * PAGE)

        # Frame 10 has two A mappings but only one holder process.
        frame10 = next(f for f in report.frames if f.frame.number == 10)
        self.assertEqual(frame10.holders, ("A",))
        self.assertEqual(frame10.holder_count, 1)
        self.assertEqual(frame10.occurrence_count, 2)
        self.assertTrue(frame10.exclusive)

        self.assertEqual(a.exclusive_frame_count, 1)
        self.assertEqual(a.exclusive_bytes, PAGE)
        self.assertEqual(a.proportional_pages, Fraction(3, 2))
        self.assertEqual(a.proportional_bytes, 3 * PAGE // 2)
        self.assertEqual(a.share_of_physical, Fraction(1, 2))

        self.assertEqual(b.exclusive_frame_count, 1)
        self.assertEqual(b.exclusive_bytes, PAGE)
        self.assertEqual(b.proportional_pages, Fraction(3, 2))
        self.assertEqual(b.proportional_bytes, 3 * PAGE // 2)

        # Proportional shares reconcile exactly to physical occupancy.
        self.assertEqual(report.total_proportional_bytes, 3 * PAGE)
        self.assertEqual(
            sum((p.share_of_physical for p in report.processes.values()), Fraction(0)),
            1,
        )

        # The naive occurrence sum is five pages, illustrating the inflation.
        naive_sum = sum(p.mapping_occurrence_bytes for p in report.processes.values())
        self.assertEqual(naive_sum, 5 * PAGE)
        self.assertGreater(naive_sum, report.physical_resident_bytes)

        # Decompose the inflation into shared-frame recounting and duplicate
        # in-process mapping occurrences.
        self.assertEqual(report.process_rss_sum_bytes, 4 * PAGE)
        self.assertEqual(report.shared_frame_overcount_bytes, PAGE)
        self.assertEqual(report.mapping_occurrence_sum_bytes, 5 * PAGE)
        self.assertEqual(report.duplicate_mapping_overcount_bytes, PAGE)

    def test_removing_one_duplicate_mapping_keeps_process_as_holder(self) -> None:
        account = MemoryAccount(page_size=PAGE)
        account.declare_mapping("A", "a1", 0, PAGE)
        account.declare_mapping("A", "a2", PAGE, 2 * PAGE)
        account.add_resident("A", pfn=10, reuse_id=1, mapping_id="a1", vaddr=0)
        account.add_resident("A", pfn=10, reuse_id=1, mapping_id="a2", vaddr=PAGE)

        removed = account.remove_resident(
            "A", pfn=10, reuse_id=1, mapping_id="a1", vaddr=0
        )
        self.assertEqual(removed, 1)
        report = account.report()
        self.assertEqual(report.physical_frame_count, 1)
        frame = report.frames[0]
        self.assertEqual(frame.holders, ("A",))
        self.assertEqual(frame.occurrence_count, 1)

        account.remove_resident(
            "A", pfn=10, reuse_id=1, mapping_id="a2", vaddr=PAGE
        )
        self.assertEqual(account.report().physical_frame_count, 0)

    def test_removing_whole_mapping_with_two_addresses(self) -> None:
        account = MemoryAccount(page_size=PAGE)
        account.declare_mapping("A", "a1", 0, 3 * PAGE)
        account.add_resident("A", pfn=10, reuse_id=1, mapping_id="a1", vaddr=0)
        account.add_resident("A", pfn=11, reuse_id=1, mapping_id="a1", vaddr=PAGE)
        account.add_resident("A", pfn=12, reuse_id=1, mapping_id="a1", vaddr=2 * PAGE)

        removed = account.remove_mapping("A", "a1")
        self.assertEqual(removed, 3)
        self.assertEqual(account.report().physical_frame_count, 0)
        self.assertNotIn(("A", "a1"), account.mappings)

    def test_cow_moves_ownership_and_share(self) -> None:
        account = MemoryAccount(page_size=PAGE)
        account.declare_mapping("A", "m", 0, PAGE)
        account.declare_mapping("B", "m", 0, PAGE)
        account.add_resident("A", pfn=7, reuse_id=1, mapping_id="m", vaddr=0)
        account.add_resident("B", pfn=7, reuse_id=1, mapping_id="m", vaddr=0)

        before = account.report()
        self.assertEqual(before.physical_frame_count, 1)
        self.assertEqual(before.processes["A"].proportional_pages, Fraction(1, 2))
        self.assertEqual(before.processes["B"].proportional_pages, Fraction(1, 2))
        self.assertEqual(before.processes["A"].exclusive_bytes, 0)

        account.copy_on_write(
            pid="A",
            mapping_id="m",
            vaddr=0,
            old_pfn=7,
            old_reuse_id=1,
            new_pfn=8,
            new_reuse_id=1,
        )
        after = account.report()

        self.assertEqual(after.physical_frame_count, 2)
        self.assertEqual({f.frame.number for f in after.frames}, {7, 8})
        old = next(f for f in after.frames if f.frame.number == 7)
        new = next(f for f in after.frames if f.frame.number == 8)
        self.assertEqual(old.holders, ("B",))
        self.assertTrue(old.exclusive)
        self.assertEqual(new.holders, ("A",))
        self.assertTrue(new.exclusive)

        self.assertEqual(after.processes["A"].exclusive_bytes, PAGE)
        self.assertEqual(after.processes["A"].proportional_bytes, PAGE)
        self.assertEqual(after.processes["B"].exclusive_bytes, PAGE)
        self.assertEqual(after.processes["B"].proportional_bytes, PAGE)
        self.assertEqual(after.total_proportional_bytes, 2 * PAGE)

    def test_cow_one_duplicate_mapping_keeps_old_holder_claim(self) -> None:
        account = MemoryAccount(page_size=PAGE)
        account.declare_mapping("A", "a1", 0, PAGE)
        account.declare_mapping("A", "a2", PAGE, 2 * PAGE)
        account.declare_mapping("B", "b1", 0, PAGE)
        account.add_resident("A", pfn=9, reuse_id=1, mapping_id="a1", vaddr=0)
        account.add_resident("A", pfn=9, reuse_id=1, mapping_id="a2", vaddr=PAGE)
        account.add_resident("B", pfn=9, reuse_id=1, mapping_id="b1", vaddr=0)

        # COW only the a1 address.  A still holds the old generation through a2.
        account.copy_on_write(
            pid="A",
            mapping_id="a1",
            vaddr=0,
            old_pfn=9,
            old_reuse_id=1,
            new_pfn=100,
            new_reuse_id=1,
        )
        report = account.report()
        old = next(f for f in report.frames if f.frame == FrameRef(9, 1))
        self.assertEqual(set(old.holders), {"A", "B"})
        self.assertEqual(old.occurrence_count, 2)
        new = next(f for f in report.frames if f.frame == FrameRef(100, 1))
        self.assertEqual(new.holders, ("A",))

    def test_frame_reuse_identity_protects_new_generation(self) -> None:
        account = MemoryAccount(page_size=PAGE)

        # New generation can arrive before the late old-generation revocation.
        account.add_resident("B", pfn=5, reuse_id=2)
        account.add_resident("A", pfn=5, reuse_id=1)
        self.assertEqual(account.report().physical_frame_count, 2)

        removed = account.revoke_frame(pfn=5, reuse_id=1)
        self.assertEqual(removed, 1)
        report = account.report()

        self.assertEqual(report.physical_frame_count, 1)
        surviving = report.frames[0]
        self.assertEqual(surviving.frame, FrameRef(5, 2))
        self.assertEqual(surviving.holders, ("B",))
        self.assertEqual(report.processes["B"].exclusive_bytes, PAGE)
        self.assertNotIn("A", report.processes)

        # A repeated/late tombstone for gen 1 is audited but cannot remove gen 2.
        again = account.revoke_frame(pfn=5, reuse_id=1)
        self.assertEqual(again, 0)
        final = account.report()
        self.assertEqual(final.physical_frame_count, 1)
        self.assertEqual(final.frames[0].frame.reuse_id, 2)
        self.assertGreaterEqual(final.stale_removal_count, 1)

    def test_pid_only_revocation_does_not_remove_other_reuse_generation(self) -> None:
        account = MemoryAccount(page_size=PAGE)
        account.add_resident("A", pfn=5, reuse_id=1)
        account.remove_resident("A", pfn=5, reuse_id=1)
        account.add_resident("B", pfn=5, reuse_id=2)

        # Late removal carrying the old generation only affects that key.
        account.remove_resident("A", pfn=5, reuse_id=1)
        report = account.report()
        self.assertEqual(report.physical_frame_count, 1)
        self.assertEqual(report.frames[0].holders, ("B",))

    def test_mapping_range_validation(self) -> None:
        strict = MemoryAccount(page_size=PAGE, strict_mappings=True)
        strict.declare_mapping("A", "m", start=0, end=PAGE)
        with self.assertRaises(MappingError):
            strict.add_resident(
                "A", pfn=1, reuse_id=1, mapping_id="m", vaddr=2 * PAGE
            )

        # The range itself is valid; the out-of-range observation was rejected.
        self.assertEqual(strict.report().physical_frame_count, 0)

    def test_strict_snapshot_validates_against_supplied_mapping_range(self) -> None:
        account = MemoryAccount(page_size=PAGE, strict_mappings=True)
        with self.assertRaises(MappingError):
            account.snapshot_process(
                {
                    "pid": "A",
                    "mappings": [{"mapping_id": "a", "start": 0, "end": PAGE}],
                    "pages": [
                        {"mapping_id": "a", "vaddr": 3 * PAGE, "pfn": 1}
                    ],
                }
            )

        account.snapshot_process(
            {
                "pid": "A",
                "mappings": [{"mapping_id": "a", "start": 0, "end": PAGE}],
                "pages": [{"mapping_id": "a", "vaddr": 0, "pfn": 1}],
            }
        )
        self.assertEqual(account.report().physical_frame_count, 1)

    def test_account_observations_dict_entrypoint(self) -> None:
        report = account_observations(
            {
                "page_size": PAGE,
                "processes": [
                    {
                        "pid": "A",
                        "mappings": [
                            {"mapping_id": "a", "start": 0, "end": 2 * PAGE}
                        ],
                        "pages": [
                            {"mapping_id": "a", "vaddr": 0, "pfn": 1, "reuse_id": 3},
                            {
                                "mapping_id": "a",
                                "vaddr": PAGE,
                                "frame": {"number": 2, "reuse_id": 3},
                            },
                        ],
                    }
                ]
            }
        )
        self.assertEqual(report.physical_frame_count, 2)
        self.assertEqual(report.processes["A"].proportional_pages, Fraction(2))

    def test_zero_observations_report_zero(self) -> None:
        report = MemoryAccount(page_size=PAGE).report()
        self.assertEqual(report.physical_frame_count, 0)
        self.assertEqual(report.physical_resident_bytes, 0)
        self.assertEqual(report.processes, {})
        self.assertEqual(report.total_proportional_bytes, 0)


if __name__ == "__main__":
    unittest.main()
