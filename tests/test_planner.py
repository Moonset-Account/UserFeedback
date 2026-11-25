import random
import sys
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memory_reservation import (  # noqa: E402
    AddressRange,
    ReservationRequest,
    UnsatisfiableReservation,
    iter_all_candidate_starts,
    plan_reservations,
    solve_assignment,
)
from memory_reservation.planner import build_conflict_report  # noqa: E402


def assert_valid_assignment(testcase, free_ranges, requests, page_size, assignment):
    testcase.assertIsNotNone(assignment)
    testcase.assertEqual(set(assignment), set(range(len(requests))))

    occupied = []
    free_tuple = tuple(free_ranges)
    for index, request in enumerate(requests):
        start = assignment[index]
        end = start + request.pages * page_size

        testcase.assertEqual(start % page_size, 0)
        if request.alignment is not None:
            testcase.assertEqual(start % request.alignment, 0)
        if request.alignment_pages is not None:
            testcase.assertEqual(start % (request.alignment_pages * page_size), 0)

        limit = request.exclusive_address_limit
        if limit is not None:
            testcase.assertLessEqual(end, limit)

        containing = [r for r in free_tuple if r.start <= start and end <= r.end]
        testcase.assertTrue(containing, f"placement {request.name} leaves free ranges")

        interval = AddressRange(start, end)
        for previous in occupied:
            testcase.assertFalse(previous.intersects(interval), "placements overlap")
        occupied.append(interval)


class BootScenarioTests(unittest.TestCase):
    PS = 0x1000

    def test_ring_boot_and_shared_receipt_are_placed_safely(self):
        # Deliberately declare the high region first, as firmware might.
        available = [
            (0x100000000, 0x101000000),
            (0x00000000, 0x00010000),
        ]
        reserved = [(0x3800, 0x3C00)]  # half of page 0x3000-0x4000

        requests = [
            {"name": "device-ring", "pages": 4, "alignment": 0x4000,
             "max_address": 0xFFFF},
            {"name": "boot-page", "pages": 1, "max_address": 0x0FFF},
            {"name": "shared-receipt", "pages": 2, "alignment_pages": 2},
        ]

        plan = plan_reservations(available, reserved, self.PS, requests)

        self.assertTrue(plan.ok)
        self.assertEqual(plan.base_address, 0)
        self.assertEqual(
            plan.start_addresses,
            {
                "device-ring": 0x4000,
                "boot-page": 0x0000,
                "shared-receipt": 0x8000,
            },
        )
        self.assertEqual(plan.allocated_pages, 7)
        # Low memory starts with 16 pages, one is reserved, and 7 are used.
        # High memory contributes another 4096 untouched pages.
        self.assertEqual(plan.remaining_pages, (16 - 1 - 7) + 4096)
        self.assertEqual(plan.largest_contiguous_pages, 4096)

        for placement in plan.placements:
            self.assertEqual(placement.start % self.PS, 0)
            self.assertFalse(
                AddressRange(0x3000, 0x4000).intersects(placement.range)
            )

    def test_low_only_device_is_found_behind_early_high_region(self):
        available = [
            (0x100000, 0x110000),  # high region declared earlier
            (0x00000, 0x02000),    # only two low pages
        ]
        requests = [
            ReservationRequest("ordinary-ring", pages=2),
            ReservationRequest("old-device", pages=1, max_address=0x0FFF),
        ]

        plan = plan_reservations(available, [], self.PS, requests)

        self.assertEqual(plan.start_addresses["old-device"], 0x0000)
        self.assertEqual(plan.start_addresses["ordinary-ring"], 0x100000)
        self.assertEqual(plan.base_address, 0)

    def test_input_order_does_not_change_constraint_aware_result(self):
        free = (AddressRange(0, 0x2000), AddressRange(0x100000, 0x110000))
        low = ReservationRequest("low", 1, max_address=0x0FFF)
        greedy = ReservationRequest("greedy", 2)

        first = solve_assignment(free, [greedy, low], self.PS)
        second = solve_assignment(free, [low, greedy], self.PS)

        self.assertEqual(first[1], 0)
        self.assertEqual(second[0], 0)
        assert_valid_assignment(self, free, [greedy, low], self.PS, first)
        assert_valid_assignment(self, free, [low, greedy], self.PS, second)


class BoundaryTests(unittest.TestCase):
    PS = 0x1000

    def test_inclusive_and_exclusive_address_boundaries(self):
        free = (AddressRange(0, 0x10000),)

        inclusive_ok = plan_reservations(
            [free[0]], [], self.PS,
            [ReservationRequest("ok", 1, max_address=0x0FFF)],
        )
        self.assertEqual(inclusive_ok.base_address, 0)

        exclusive_ok = plan_reservations(
            [free[0]], [], self.PS,
            [ReservationRequest("ok", 1, address_limit=0x1000)],
        )
        self.assertEqual(exclusive_ok.base_address, 0)

        with self.assertRaises(UnsatisfiableReservation) as ctx:
            plan_reservations(
                [free[0]], [], self.PS,
                [ReservationRequest("too-low", 1, max_address=0x0FFE)],
            )
        self.assertEqual(ctx.exception.report.blockers, ("too-low",))

    def test_address_bits_are_exclusive_power_of_two_limit(self):
        request = ReservationRequest("dma", 1, address_bits=32)
        self.assertEqual(request.exclusive_address_limit, 1 << 32)

        plan = plan_reservations(
            [(0x1_0000_0000, 0x1_0001_0000), (0, 0x10000)],
            [],
            self.PS,
            [request],
        )
        self.assertEqual(plan.base_address, 0)

    def test_alignment_gap_is_usable_by_smaller_request(self):
        # First free page starts at 0x1000; 16 KiB alignment jumps to 0x4000.
        free = (AddressRange(0x1000, 0x9000),)
        requests = [
            ReservationRequest("aligned-big", 4, alignment=0x4000),
            ReservationRequest("small", 2),
        ]
        assignment = solve_assignment(free, requests, self.PS)
        assert_valid_assignment(self, free, requests, self.PS, assignment)
        self.assertEqual(assignment[0], 0x4000)
        self.assertEqual(assignment[1], 0x1000)


class ConflictReportTests(unittest.TestCase):
    PS = 0x1000

    def test_pair_that_cannot_fit_keeps_names(self):
        free = (AddressRange(0, 4 * self.PS),)
        requests = [
            ReservationRequest("ring", 3),
            ReservationRequest("boot", 2),
        ]

        report = build_conflict_report(requests, free, self.PS)
        self.assertEqual(report.blockers, ("ring", "boot"))
        self.assertEqual(report.incompatible_pairs, (("ring", "boot"),))
        self.assertIn("互相卡住", report.message)

        with self.assertRaises(UnsatisfiableReservation) as ctx:
            plan_reservations([(0, 4 * self.PS)], [], self.PS, requests)
        self.assertEqual(ctx.exception.report.blockers, ("ring", "boot"))

    def test_three_way_collective_conflict_has_minimal_set(self):
        free = (AddressRange(0, 4 * self.PS),)
        requests = [
            ReservationRequest("a", 2),
            ReservationRequest("b", 2),
            ReservationRequest("c", 1),
        ]

        report = build_conflict_report(requests, free, self.PS)
        self.assertEqual(set(report.blockers), {"a", "b", "c"})
        self.assertEqual(report.incompatible_pairs, ())

        # The reported set is unsatisfiable, but removing any one blocker
        # makes the remaining subset satisfiable.
        by_name = {request.name: request for request in requests}
        blocker_requests = [by_name[name] for name in report.blockers]
        self.assertIsNone(solve_assignment(free, blocker_requests, self.PS))
        for removed in report.blockers:
            subset = [by_name[name] for name in report.blockers if name != removed]
            self.assertIsNotNone(solve_assignment(free, subset, self.PS))

    def test_single_request_contiguity_failure_is_diagnosed(self):
        free = (
            AddressRange(0, 2 * self.PS),
            AddressRange(0x10000, 0x10000 + 2 * self.PS),
        )
        requests = [ReservationRequest("huge-ring", 3)]
        report = build_conflict_report(requests, free, self.PS)
        self.assertEqual(report.blockers, ("huge-ring",))
        self.assertFalse(
            report.diagnostics["huge-ring"]["can_place_alone"]
        )
        self.assertIn("连续", report.reasons["huge-ring"])


class ExactSolverRandomizedTests(unittest.TestCase):
    """Cross-check the canonical solver against exhaustive small search."""

    def brute_force_feasible(self, free, requests, page_size):
        candidates = [
            tuple(iter_all_candidate_starts(request, free, page_size))
            for request in requests
        ]
        if any(not options for options in candidates):
            return False

        occupied = []

        def visit(index):
            if index == len(requests):
                return True
            request = requests[index]
            end_length = request.pages * page_size
            for start in candidates[index]:
                candidate_interval = AddressRange(start, start + end_length)
                if any(used.intersects(candidate_interval) for used in occupied):
                    continue
                occupied.append(candidate_interval)
                if visit(index + 1):
                    return True
                occupied.pop()
            return False

        return visit(0)

    def test_maps_and_constraints_against_brute_force(self):
        rng = random.Random(20261126)
        page_size = 1

        for seed in range(250):
            rng.seed(9000 + seed)
            total_pages = rng.randint(6, 14)

            free = []
            cursor = 0
            while cursor < total_pages:
                gap = rng.randint(0, 2)
                cursor += gap
                run = rng.randint(1, 4)
                if cursor < total_pages:
                    end = min(total_pages, cursor + run)
                    free.append(AddressRange(cursor, end))
                    cursor = end
                else:
                    break

            request_count = rng.randint(1, 5)
            requests = []
            for index in range(request_count):
                alignment = rng.choice([1, 1, 2, 2, 4])
                limit = rng.choice([None, None, total_pages, total_pages - 1, 4, 6])
                requests.append(
                    ReservationRequest(
                        name=f"r{index}",
                        pages=rng.randint(1, 3),
                        alignment=alignment,
                        address_limit=limit,
                    )
                )

            assignment = solve_assignment(free, requests, page_size)
            brute = self.brute_force_feasible(free, requests, page_size)

            if brute:
                assert_valid_assignment(
                    self, free, requests, page_size, assignment
                )
            else:
                self.assertIsNone(
                    assignment,
                    f"solver claimed infeasible case was feasible: seed={seed}",
                )

            if assignment is None:
                report = build_conflict_report(requests, free, page_size)
                self.assertTrue(report.blockers)
                by_name = {request.name: request for request in requests}
                blockers = [by_name[name] for name in report.blockers]
                self.assertIsNone(solve_assignment(free, blockers, page_size))

                # Deletion-based minimization: removing any reported blocker
                # must make the remaining blocking subset feasible.
                for removed_name in report.blockers:
                    subset = [
                        by_name[name]
                        for name in report.blockers
                        if name != removed_name
                    ]
                    self.assertIsNotNone(solve_assignment(free, subset, page_size))


if __name__ == "__main__":
    unittest.main()
