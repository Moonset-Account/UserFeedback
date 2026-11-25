import sys
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memory_reservation import (  # noqa: E402
    ReservationRequest,
    ReservationTable,
    UnsatisfiableReservation,
    plan_reservations,
    reserve_ranges,
)


class ReservationTableTests(unittest.TestCase):
    PS = 4096

    def test_successful_group_commits_all_names_and_pages(self):
        table = ReservationTable(
            available_ranges=[(0, 0x10000)],
            reserved_ranges=[(0x1800, 0x2000)],  # removes page 0x1000-0x2000
            page_size=self.PS,
        )
        self.assertEqual(table.remaining_pages, 15)

        plan = table.reserve_group(
            [
                {"name": "ring", "pages": 2, "alignment_pages": 2},
                {"name": "ack", "pages": 1},
            ]
        )

        self.assertTrue(plan.ok)
        self.assertEqual(plan.start_addresses["ring"], 0x2000)
        self.assertEqual(plan.start_addresses["ack"], 0x0000)
        self.assertEqual(table.remaining_pages, 12)
        self.assertIn("ring", table.allocation_addresses)
        self.assertEqual(table.base_address, 0)

    def test_failed_group_preserves_exact_prior_state(self):
        table = ReservationTable.from_free_ranges(
            [(0, 4 * self.PS)], page_size=self.PS
        )
        table.reserve("existing", pages=1)
        before_free = table.free_ranges
        before_addresses = dict(table.allocation_addresses)
        before_remaining = table.remaining_pages

        outcome = table.try_reserve_group(
            [("a", 3), ("b", 2)]  # 5 pages requested from 3 free
        )

        self.assertFalse(outcome.ok)
        self.assertEqual(set(outcome.blockers), {"a", "b"})
        self.assertEqual(table.free_ranges, before_free)
        self.assertEqual(table.allocation_addresses, before_addresses)
        self.assertEqual(table.remaining_pages, before_remaining)
        self.assertEqual(table.remaining_pages, 3)

        with self.assertRaises(UnsatisfiableReservation):
            table.reserve_group([("still-bad", 99)])
        self.assertEqual(table.remaining_pages, 3)

    def test_duplicate_name_does_not_overwrite(self):
        table = ReservationTable.from_free_ranges([(0, 10 * self.PS)], self.PS)
        original = table.reserve("ring", pages=1, address_limit=0x1000)

        outcome = table.try_reserve_group([{"name": "ring", "pages": 4}])

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.blockers, ("ring",))
        self.assertEqual(table.placement_for("ring"), original)
        self.assertEqual(table.placement_for("ring").pages, 1)

    def test_release_restores_mergeable_free_space(self):
        table = ReservationTable.from_free_ranges([(0, 4 * self.PS)], self.PS)
        placement = table.reserve("temp", pages=2)
        self.assertEqual(table.remaining_pages, 2)

        released = table.release("temp")
        self.assertEqual(released, placement)
        self.assertEqual(table.remaining_pages, 4)
        self.assertEqual(table.largest_contiguous_pages, 4)

    def test_snapshot_and_restore(self):
        table = ReservationTable.from_free_ranges([(0, 8 * self.PS)], self.PS)
        table.reserve("a", pages=2)
        snapshot = table.snapshot()
        table.reserve("b", pages=1)
        self.assertEqual(table.remaining_pages, 5)

        table.restore(snapshot)
        self.assertEqual(table.remaining_pages, 6)
        self.assertEqual(set(table.allocation_addresses), {"a"})


class InputValidationTests(unittest.TestCase):
    def test_invalid_page_size_and_requests(self):
        with self.assertRaises(ValueError):
            plan_reservations([(0, 0x1000)], [], 0, [("x", 1)])
        with self.assertRaises(ValueError):
            ReservationRequest("x", 0)
        with self.assertRaises(ValueError):
            ReservationRequest("x", 1, alignment=-4096)

    def test_duplicate_request_names_are_rejected_before_mutation(self):
        table = ReservationTable.from_free_ranges([(0, 10 * 4096)], 4096)
        with self.assertRaises(ValueError):
            table.reserve_group([("same", 1), ("same", 2)])
        self.assertEqual(table.remaining_pages, 10)
        self.assertEqual(table.allocation_addresses, {})


class FunctionalApiTests(unittest.TestCase):
    def test_reserve_ranges_success_dictionary(self):
        result = reserve_ranges(
            available_ranges=[{"base": 0, "length": 0x10000}],
            reserved_ranges=[],
            page_size=4096,
            requests=[{"name": "boot", "pages": 1}],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["base_address"], 0)
        self.assertEqual(result["remaining_pages"], 15)

    def test_reserve_ranges_conflict_dictionary_by_default(self):
        result = reserve_ranges(
            available_ranges=[(0, 0x1000)],
            reserved_ranges=[],
            page_size=4096,
            requests=[{"name": "too-big", "pages": 2}],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["blockers"], ["too-big"])

    def test_reserve_ranges_can_raise(self):
        with self.assertRaises(UnsatisfiableReservation):
            reserve_ranges(
                available_ranges=[(0, 0x1000)],
                reserved_ranges=[],
                page_size=4096,
                requests=[{"name": "too-big", "pages": 2}],
                raise_on_conflict=True,
            )


if __name__ == "__main__":
    unittest.main()
