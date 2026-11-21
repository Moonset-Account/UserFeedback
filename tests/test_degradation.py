"""Tests for the capacity-constrained degradation planner.

The suite covers:

* normal operation (best tiers, sharing, dependency charges, tie-breaks);
* boundaries (capacity 0, exactly 10 entries, 3 tiers, floors, empty input);
* interleaved scenarios that combine floors, sharing, dependencies and both
  resources;
* input validation and structured errors;
* a randomised cross-check against an independent reference enumeration.

Run from the workspace root with::

    python3 -m unittest discover -v
"""

from __future__ import annotations

import itertools
import random
import time
import unittest

import degradation as deg
from degradation import (
    Component,
    Entry,
    InfeasiblePlanError,
    InputError,
    Selection,
    Tier,
    solve,
)


def _plan_is_consistent(test: unittest.TestCase, plan: deg.Plan, entries: list[Entry],
                        components: list[Component], cpu_capacity: int,
                        memory_capacity: int) -> None:
    """Recompute a plan's accounting from first principles."""

    by_name = {component.name: component for component in components}
    entry_map = {entry.name: entry for entry in entries}

    private_cpu = 0
    private_memory = 0
    benefit = 0
    needed: set[str] = set()

    test.assertEqual(tuple(selection.entry for selection in plan.selections),
                     tuple(entry.name for entry in entries))

    for selection in plan.selections:
        entry = entry_map[selection.entry]
        test.assertIsInstance(selection, Selection)
        test.assertTrue(0 <= selection.tier_index < len(entry.tiers))
        # Compare by position, not by value: two tiers may be equal.
        test.assertIs(selection.tier, entry.tiers[selection.tier_index])
        test.assertLessEqual(selection.tier_index, entry.floor)
        chosen = entry.tiers[selection.tier_index]
        benefit += chosen.benefit
        private_cpu += chosen.cpu
        private_memory += chosen.memory
        for name in chosen.components:
            needed.add(name)

    # Transitive closure of everything the chosen tiers require.
    stack = list(needed)
    while stack:
        name = stack.pop()
        for dependency in by_name[name].requires:
            if dependency not in needed:
                needed.add(dependency)
                stack.append(dependency)

    test.assertEqual(plan.enabled_components, frozenset(needed))
    shared_cpu = sum(by_name[name].cpu for name in needed)
    shared_memory = sum(by_name[name].memory for name in needed)

    test.assertEqual(plan.total_benefit, benefit)
    test.assertEqual(plan.private_cpu, private_cpu)
    test.assertEqual(plan.private_memory, private_memory)
    test.assertEqual(plan.component_cpu, shared_cpu)
    test.assertEqual(plan.component_memory, shared_memory)
    test.assertEqual(plan.total_cpu, private_cpu + shared_cpu)
    test.assertEqual(plan.total_memory, private_memory + shared_memory)
    test.assertLessEqual(plan.total_cpu, cpu_capacity)
    test.assertLessEqual(plan.total_memory, memory_capacity)


# ---------------------------------------------------------------------------
# An independent reference implementation used only for cross-checking.
# ---------------------------------------------------------------------------


def _reference_solve(entries: list[Entry], components: list[Component],
                     cpu_capacity: int, memory_capacity: int):
    """Return ``(benefit, cpu, memory, choice)`` of the reference optimum.

    ``choice`` is the lexicographically smallest tier-index vector among the
    plans attaining the minimal CPU, matching the documented tie-break.
    """

    by_name = {component.name: component for component in components}

    memo: dict[str, frozenset[str]] = {}

    def closure(name: str, path: frozenset[str]) -> frozenset[str]:
        if name in memo:
            return memo[name]
        result = {name}
        for dependency in by_name[name].requires:
            if dependency in path:
                raise AssertionError("cycle in reference input")
            result |= closure(dependency, path | {name})
        computed = frozenset(result)
        memo[name] = computed
        return computed

    best = None
    allowed = [range(entry.floor + 1) for entry in entries]
    for choice in itertools.product(*allowed):
        benefit = 0
        private_cpu = 0
        private_memory = 0
        needed: set[str] = set()
        for entry, tier_index in zip(entries, choice):
            tier = entry.tiers[tier_index]
            benefit += tier.benefit
            private_cpu += tier.cpu
            private_memory += tier.memory
            for name in tier.components:
                needed |= set(closure(name, frozenset()))
        total_cpu = private_cpu + sum(by_name[name].cpu for name in needed)
        total_memory = private_memory + sum(by_name[name].memory for name in needed)
        if total_cpu > cpu_capacity or total_memory > memory_capacity:
            continue
        candidate = (benefit, total_cpu, total_memory, tuple(choice))
        if best is None:
            best = candidate
        elif candidate[0] > best[0] or (
            candidate[0] == best[0] and candidate[1] < best[1]
        ):
            best = candidate
        # ``itertools.product`` yields vectors in lexicographic order, and
        # strictly-better comparisons never replace on a tie, so the first
        # optimal vector for a given (benefit, cpu) is lexicographically first.
    return best


class TestNormalOperation(unittest.TestCase):
    def test_generous_capacity_keeps_best_tiers(self) -> None:
        entries = [
            Entry("rec", (Tier(100, 4, 4), Tier(60, 1, 1), Tier(10, 0, 0))),
            Entry("search", (Tier(90, 3, 3), Tier(50, 1, 1), Tier(5, 0, 0))),
        ]
        plan = solve(entries, cpu_capacity=100, memory_capacity=100)
        self.assertEqual(plan.choices, {"rec": 0, "search": 0})
        self.assertEqual(plan.total_benefit, 190)
        self.assertEqual(plan.total_cpu, 7)
        self.assertEqual(plan.total_memory, 7)
        self.assertEqual(plan.enabled_components, frozenset())
        _plan_is_consistent(self, plan, entries, [], 100, 100)

    def test_shared_component_is_charged_once(self) -> None:
        entries = [
            Entry("rec", (Tier(100, 4, 4, ("vec",)), Tier(60, 1, 1))),
            Entry("search", (Tier(90, 3, 3, ("vec",)), Tier(50, 1, 1))),
        ]
        components = [Component("vec", 6, 6)]
        plan = solve(entries, components, cpu_capacity=100, memory_capacity=100)
        self.assertEqual(plan.choices, {"rec": 0, "search": 0})
        self.assertEqual(plan.total_benefit, 190)
        # private 4 + 3, shared 6 exactly once (not 12).
        self.assertEqual(plan.private_cpu, 7)
        self.assertEqual(plan.component_cpu, 6)
        self.assertEqual(plan.total_cpu, 13)
        self.assertEqual(plan.enabled_components, frozenset({"vec"}))
        _plan_is_consistent(self, plan, entries, components, 100, 100)

    def test_degradation_frees_shared_component_only_for_last_user(self) -> None:
        # Two entries share ``c``.  At CPU 9 neither single user fits and the
        # component cost only disappears once both have left it.
        entries = [
            Entry("a", (Tier(100, 0, 0, ("c",)), Tier(99, 0, 0))),
            Entry("b", (Tier(50, 0, 0, ("c",)), Tier(49, 0, 0))),
        ]
        components = [Component("c", 10, 0)]
        plan = solve(entries, components, cpu_capacity=9, memory_capacity=100)
        self.assertEqual(plan.choices, {"a": 1, "b": 1})
        self.assertEqual(plan.enabled_components, frozenset())
        self.assertEqual(plan.total_cpu, 0)
        self.assertEqual(plan.total_benefit, 148)

        # One step up, both users fit and the shared cost is paid once.
        plan = solve(entries, components, cpu_capacity=10, memory_capacity=100)
        self.assertEqual(plan.choices, {"a": 0, "b": 0})
        self.assertEqual(plan.component_cpu, 10)
        self.assertEqual(plan.total_cpu, 10)
        self.assertEqual(plan.total_benefit, 150)
        _plan_is_consistent(self, plan, entries, components, 10, 100)

    def test_component_kept_while_one_user_remains(self) -> None:
        # Degrading ``a`` while ``b`` still needs ``c`` does not reclaim the
        # shared cost, so the cheaper plan is not automatically the one with
        # more degradation.
        entries = [
            Entry("a", (Tier(100, 1, 0, ("c",)), Tier(10, 0, 0))),
            Entry("b", (Tier(50, 0, 0, ("c",)),)),
        ]
        components = [Component("c", 5, 0)]
        plan = solve(entries, components, cpu_capacity=6, memory_capacity=10)
        self.assertEqual(plan.choices, {"a": 0, "b": 0})
        self.assertEqual(plan.component_cpu, 5)
        self.assertEqual(plan.total_cpu, 6)
        self.assertEqual(plan.total_benefit, 150)

        # With the component still enabled by ``b``, dropping only ``a``
        # cannot help: at CPU 5 the best plan drops both.
        plan = solve(entries, components, cpu_capacity=5, memory_capacity=10)
        self.assertEqual(plan.choices, {"a": 1, "b": 0})
        self.assertEqual(plan.enabled_components, frozenset({"c"}))
        self.assertEqual(plan.total_cpu, 5)

    def test_dependency_costs_are_added_transitively(self) -> None:
        comps = [
            Component("base", 3, 1),
            Component("vec", 2, 1, ("base",)),
        ]
        entries = [Entry("rec", (Tier(10, 0, 0, ("vec",)), Tier(0, 0, 0)))]
        plan = solve(entries, comps, cpu_capacity=5, memory_capacity=10)
        self.assertEqual(plan.choices, {"rec": 0})
        self.assertEqual(plan.enabled_components, frozenset({"vec", "base"}))
        self.assertEqual(plan.component_cpu, 5)

        plan = solve(entries, comps, cpu_capacity=4, memory_capacity=10)
        self.assertEqual(plan.choices, {"rec": 1})
        self.assertEqual(plan.enabled_components, frozenset())
        self.assertEqual(plan.total_cpu, 0)

    def test_diamond_dependency_is_not_double_counted(self) -> None:
        comps = [
            Component("base", 1, 0),
            Component("left", 1, 0, ("base",)),
            Component("right", 1, 0, ("base",)),
            Component("top", 1, 0, ("left", "right")),
        ]
        entries = [Entry("e", (Tier(10, 0, 0, ("top",)), Tier(0, 0, 0)))]
        plan = solve(entries, comps, cpu_capacity=4, memory_capacity=10)
        self.assertEqual(plan.choices, {"e": 0})
        self.assertEqual(plan.enabled_components, frozenset({"top", "left", "right", "base"}))
        self.assertEqual(plan.component_cpu, 4)

        plan = solve(entries, comps, cpu_capacity=3, memory_capacity=10)
        self.assertEqual(plan.choices, {"e": 1})

    def test_unused_components_are_not_enabled_or_charged(self) -> None:
        comps = [Component("expensive", 1000, 1000), Component("cheap", 1, 1)]
        entries = [Entry("e", (Tier(10, 0, 0, ("cheap",)),))]
        plan = solve(entries, comps, cpu_capacity=1, memory_capacity=1)
        self.assertEqual(plan.enabled_components, frozenset({"cheap"}))
        self.assertEqual(plan.total_cpu, 1)
        self.assertEqual(plan.total_memory, 1)

    def test_tie_on_benefit_prefers_lower_total_cpu(self) -> None:
        entries = [Entry("e", (Tier(100, 5, 1), Tier(100, 2, 1)))]
        plan = solve(entries, cpu_capacity=10, memory_capacity=10)
        self.assertEqual(plan.choices, {"e": 1})
        self.assertEqual(plan.total_benefit, 100)
        self.assertEqual(plan.total_cpu, 2)

    def test_cross_entry_tie_is_broken_deterministically(self) -> None:
        # All four vectors yield benefit 100.  With component ``c`` costing 2:
        #   (0, 0) -> private 2, c enabled -> CPU 4
        #   (0, 1) -> private 0, c enabled -> CPU 2
        #   (1, 0) -> private 4, no c     -> CPU 4
        #   (1, 1) -> private 2, no c     -> CPU 2
        # Minimal CPU 2 is shared by (0, 1) and (1, 1); the lexicographically
        # first tier-index vector in input order wins: (0, 1).
        entries = [
            Entry("a", (Tier(50, 0, 0, ("c",)), Tier(50, 2, 0))),
            Entry("b", (Tier(50, 2, 0), Tier(50, 0, 0))),
        ]
        components = [Component("c", 2, 0)]
        for _ in range(5):
            plan = solve(entries, components, cpu_capacity=10, memory_capacity=10)
            self.assertEqual(plan.choices, {"a": 0, "b": 1})
            self.assertEqual(plan.total_benefit, 100)
            self.assertEqual(plan.total_cpu, 2)
            self.assertEqual(plan.enabled_components, frozenset({"c"}))

    def test_minimum_cpu_beats_keeping_shared_component(self) -> None:
        # Every tier gives benefit 50, so total benefit is always 100.  Leaving
        # the shared component entirely costs 0 CPU, which beats any plan that
        # keeps it enabled.
        entries = [
            Entry("a", (Tier(50, 0, 0, ("c",)), Tier(50, 0, 0))),
            Entry("b", (Tier(50, 0, 0, ("c",)), Tier(50, 0, 0))),
        ]
        components = [Component("c", 4, 0)]
        plan = solve(entries, components, cpu_capacity=4, memory_capacity=10)
        self.assertEqual(plan.choices, {"a": 1, "b": 1})
        self.assertEqual(plan.total_cpu, 0)
        self.assertEqual(plan.enabled_components, frozenset())

    def test_lower_tier_may_have_higher_benefit(self) -> None:
        # The solver takes benefits literally instead of assuming monotonicity.
        entries = [Entry("e", (Tier(10, 1, 0), Tier(90, 1, 0)))]
        plan = solve(entries, cpu_capacity=10, memory_capacity=10)
        self.assertEqual(plan.choices, {"e": 1})
        self.assertEqual(plan.total_benefit, 90)

    def test_deep_dependency_chain_is_charged_fully(self) -> None:
        comps = [Component(f"c{i}", 1, 0, (f"c{i - 1}",) if i else ()) for i in range(5)]
        entries = [Entry("e", (Tier(10, 0, 0, ("c4",)), Tier(0, 0, 0)))]
        plan = solve(entries, comps, cpu_capacity=5, memory_capacity=0)
        self.assertEqual(plan.choices, {"e": 0})
        self.assertEqual(plan.enabled_components, frozenset({f"c{i}" for i in range(5)}))
        self.assertEqual(plan.component_cpu, 5)

        plan = solve(entries, comps, cpu_capacity=4, memory_capacity=0)
        self.assertEqual(plan.choices, {"e": 1})
        self.assertEqual(plan.enabled_components, frozenset())

    def test_component_declaration_order_does_not_matter(self) -> None:
        # ``top`` is declared before its dependency.
        comps = [
            Component("top", 2, 0, ("leaf",)),
            Component("leaf", 3, 1),
        ]
        entries = [Entry("e", (Tier(10, 0, 0, ("top",)), Tier(0, 0, 0)))]
        plan = solve(entries, comps, cpu_capacity=5, memory_capacity=1)
        self.assertEqual(plan.enabled_components, frozenset({"top", "leaf"}))
        self.assertEqual(plan.total_cpu, 5)
        self.assertEqual(plan.total_memory, 1)

        plan = solve(entries, comps, cpu_capacity=4, memory_capacity=1)
        self.assertEqual(plan.choices, {"e": 1})

    def test_component_needed_by_two_tiers_of_same_entry_is_charged_once(self) -> None:
        comps = [Component("shared", 5, 5)]
        # Both tiers of one entry use ``shared``; only one tier is selected, so
        # it is charged once regardless of how many tiers reference it.
        entries = [Entry("e", (Tier(10, 0, 0, ("shared",)),
                               Tier(5, 0, 0, ("shared",))))]
        plan = solve(entries, comps, cpu_capacity=5, memory_capacity=5)
        self.assertEqual(plan.total_cpu, 5)
        self.assertEqual(plan.enabled_components, frozenset({"shared"}))

    def test_interleaved_mixed_floors_sharing_and_dependencies(self) -> None:
        comps = [
            Component("base", 1, 1),
            Component("vec", 2, 3, ("base",)),
            Component("cache", 1, 0),
        ]
        entries = [
            Entry(
                "rec",
                (
                    Tier(100, 2, 2, ("vec",)),
                    Tier(70, 1, 1, ("cache",)),
                    Tier(20, 0, 0),
                ),
            ),
            Entry(
                "search",
                (
                    Tier(80, 1, 1, ("vec",)),
                    Tier(60, 1, 1),
                    Tier(10, 0, 0),
                ),
                floor=1,
            ),
            Entry(
                "ads",
                (
                    Tier(40, 0, 0, ("cache", "vec")),
                    Tier(25, 0, 0, ("cache",)),
                ),
            ),
            Entry("cdn", (Tier(30, 1, 1),), floor=0),
        ]
        # Very tight memory forces dropping the vec pipeline while keeping the
        # cheap cache tier and honoring every floor.  Reference optimum:
        # (185 benefit, 4 CPU, 3 memory, choice (1, 1, 1, 0)).
        plan = solve(entries, comps, cpu_capacity=100, memory_capacity=4)
        self.assertLessEqual(plan.total_memory, 4)
        self.assertEqual(plan.total_benefit, 185)
        self.assertEqual(plan.total_cpu, 4)
        self.assertEqual(plan.choices, {"rec": 1, "search": 1, "ads": 1, "cdn": 0})
        self.assertEqual(plan.enabled_components, frozenset({"cache"}))
        for entry in entries:
            self.assertLessEqual(plan.tier_index(entry.name), entry.floor)
        _plan_is_consistent(self, plan, entries, comps, 100, 4)



    def test_single_tier_entry_cannot_degrade(self) -> None:
        entries = [
            Entry("fixed", (Tier(30, 2, 2),)),
            Entry("flex", (Tier(100, 9, 9), Tier(10, 0, 0))),
        ]
        plan = solve(entries, cpu_capacity=2, memory_capacity=2)
        self.assertEqual(plan.choices, {"fixed": 0, "flex": 1})
        self.assertEqual(plan.total_benefit, 40)

    def test_plan_accessors(self) -> None:
        entries = [Entry("e", (Tier(1, 0, 0), Tier(2, 0, 0)))]
        plan = solve(entries, cpu_capacity=0, memory_capacity=0)
        self.assertEqual(plan.tier_index("e"), 1)
        self.assertEqual(plan.choices, {"e": 1})
        with self.assertRaises(KeyError):
            plan.tier_index("missing")

    def test_identical_tiers_report_the_positional_index(self) -> None:
        # Two tiers are value-equal; the plan must still report the index that
        # was actually chosen (identity, not ``tuple.index``).
        first = Tier(5, 1, 0)
        second = Tier(5, 1, 0)
        entries = [Entry("e", (first, second))]
        plan = solve(entries, cpu_capacity=10, memory_capacity=0)
        self.assertEqual(plan.choices, {"e": 0})
        self.assertIs(plan.selections[0].tier, first)
        self.assertEqual(entries[0].tiers.index(plan.selections[0].tier), 0)


class TestBoundaries(unittest.TestCase):
    def test_zero_capacity_uses_free_tiers(self) -> None:
        entries = [
            Entry("a", (Tier(10, 0, 0), Tier(5, 0, 0))),
            Entry("b", (Tier(20, 0, 0), Tier(1, 0, 0))),
        ]
        plan = solve(entries, cpu_capacity=0, memory_capacity=0)
        self.assertEqual(plan.choices, {"a": 0, "b": 0})
        self.assertEqual(plan.total_cpu, 0)

    def test_zero_capacity_with_positive_floor_costs_is_infeasible(self) -> None:
        # floor=0 pins the entry to its 1-CPU tier.
        entries = [Entry("a", (Tier(10, 1, 0), Tier(5, 0, 0)), floor=0)]
        with self.assertRaises(InfeasiblePlanError):
            solve(entries, cpu_capacity=0, memory_capacity=0)

    def test_unreachable_shared_component_is_infeasible_at_zero_capacity(self) -> None:
        entries = [Entry("a", (Tier(10, 0, 0, ("c",)), Tier(5, 0, 0)), floor=0)]
        with self.assertRaises(InfeasiblePlanError):
            solve(entries, [Component("c", 1, 0)],
                  cpu_capacity=0, memory_capacity=0)

    def test_floor_zero_prevents_degradation(self) -> None:
        entries = [Entry("a", (Tier(10, 1, 1), Tier(9, 0, 0)), floor=0)]
        plan = solve(entries, cpu_capacity=1, memory_capacity=1)
        self.assertEqual(plan.choices, {"a": 0})
        with self.assertRaises(InfeasiblePlanError):
            solve(entries, cpu_capacity=0, memory_capacity=1)

    def test_floor_allows_partial_degradation(self) -> None:
        entries = [
            Entry("a", (Tier(10, 3, 3), Tier(8, 2, 2), Tier(1, 0, 0)), floor=1),
        ]
        plan = solve(entries, cpu_capacity=2, memory_capacity=2)
        self.assertEqual(plan.choices, {"a": 1})
        self.assertEqual(plan.total_benefit, 8)

    def test_floor_defaults_to_last_tier(self) -> None:
        entries = [Entry("a", (Tier(10, 3, 0), Tier(8, 2, 0), Tier(1, 0, 0)))]
        self.assertEqual(entries[0].floor, 2)
        plan = solve(entries, cpu_capacity=0, memory_capacity=0)
        self.assertEqual(plan.choices, {"a": 2})

    def test_exactly_ten_entries_is_allowed(self) -> None:
        entries = [
            Entry(f"e{i}", (Tier(10, 1, 1), Tier(5, 0, 0), Tier(1, 0, 0)))
            for i in range(10)
        ]
        plan = solve(entries, cpu_capacity=10, memory_capacity=10)
        self.assertEqual(plan.total_benefit, 100)
        self.assertEqual(len(plan.selections), 10)
        _plan_is_consistent(self, plan, entries, [], 10, 10)

    def test_exactly_three_tiers_is_allowed(self) -> None:
        entry = Entry("a", (Tier(3, 0, 0), Tier(2, 0, 0), Tier(1, 0, 0)))
        self.assertEqual(len(entry.tiers), 3)

    def test_four_tiers_rejected(self) -> None:
        with self.assertRaises(InputError):
            Entry("a", (Tier(4, 0, 0), Tier(3, 0, 0), Tier(2, 0, 0), Tier(1, 0, 0)))

    def test_eleven_entries_rejected(self) -> None:
        entries = [Entry(f"e{i}", (Tier(1, 0, 0),)) for i in range(11)]
        with self.assertRaises(InputError):
            solve(entries, cpu_capacity=10, memory_capacity=10)

    def test_empty_entries_yield_empty_plan(self) -> None:
        plan = solve([], cpu_capacity=0, memory_capacity=0)
        self.assertEqual(plan.selections, ())
        self.assertEqual(plan.total_benefit, 0)
        self.assertEqual(plan.total_cpu, 0)
        self.assertEqual(plan.enabled_components, frozenset())

    def test_negative_benefit_is_preserved(self) -> None:
        entries = [Entry("a", (Tier(-5, 0, 0),))]
        plan = solve(entries, cpu_capacity=0, memory_capacity=0)
        self.assertEqual(plan.total_benefit, -5)

    def test_ten_entries_three_tiers_completes_quickly(self) -> None:
        entries = [
            Entry(
                f"e{i}",
                (
                    Tier(100 + i, 3, 3, ("shared",) if i % 2 == 0 else ()),
                    Tier(50 + i, 1, 1, ("shared",) if i % 3 == 0 else ()),
                    Tier(i, 0, 0),
                ),
            )
            for i in range(10)
        ]
        components = [Component("shared", 20, 20)]
        started = time.monotonic()
        plan = solve(entries, components, cpu_capacity=40, memory_capacity=40)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 20.0)
        _plan_is_consistent(self, plan, entries, components, 40, 40)


class TestInterleavedScenarios(unittest.TestCase):
    def _scenario(self):
        components = [
            Component("base", 2, 2),
            Component("vec", 3, 4, ("base",)),
            Component("cache", 1, 1),
        ]
        entries = [
            Entry(
                "rec",
                (
                    Tier(100, 2, 2, ("vec",)),
                    Tier(70, 1, 1, ("cache",)),
                    Tier(20, 0, 0),
                ),
            ),
            Entry(
                "search",
                (
                    Tier(80, 1, 1, ("vec",)),
                    Tier(60, 1, 1),
                    Tier(10, 0, 0),
                ),
            ),
            Entry("ads", (Tier(30, 1, 2),), floor=0),
        ]
        return entries, components

    def test_roomy_capacity_takes_every_best_tier(self) -> None:
        entries, components = self._scenario()
        plan = solve(entries, components, cpu_capacity=100, memory_capacity=100)
        self.assertEqual(plan.choices, {"rec": 0, "search": 0, "ads": 0})
        self.assertEqual(plan.total_benefit, 210)
        self.assertEqual(plan.enabled_components, frozenset({"vec", "base"}))
        self.assertEqual(plan.component_cpu, 5)
        self.assertEqual(plan.total_cpu, 9)
        self.assertEqual(plan.total_memory, 11)
        _plan_is_consistent(self, plan, entries, components, 100, 100)

    def test_memory_pressure_mixes_dependency_drop_and_degradation(self) -> None:
        entries, components = self._scenario()
        plan = solve(entries, components, cpu_capacity=100, memory_capacity=10)
        # Dropping the shared ``vec`` pipeline frees 6 memory at once; keeping
        # the cheap ``cache`` tier beats every alternative.
        self.assertEqual(plan.choices, {"rec": 1, "search": 1, "ads": 0})
        self.assertEqual(plan.total_benefit, 160)
        self.assertEqual(plan.total_memory, 5)
        self.assertEqual(plan.enabled_components, frozenset({"cache"}))
        _plan_is_consistent(self, plan, entries, components, 100, 10)

    def test_cpu_pressure_alone(self) -> None:
        entries, components = self._scenario()
        plan = solve(entries, components, cpu_capacity=8, memory_capacity=100)
        self.assertEqual(plan.choices, {"rec": 1, "search": 1, "ads": 0})
        self.assertEqual(plan.total_benefit, 160)
        self.assertEqual(plan.total_cpu, 4)
        _plan_is_consistent(self, plan, entries, components, 8, 100)

    def test_tight_cpu_forces_degradation(self) -> None:
        entries, components = self._scenario()
        plan = solve(entries, components, cpu_capacity=5, memory_capacity=100)
        self.assertLessEqual(plan.total_cpu, 5)
        self.assertEqual(plan.total_benefit, 160)
        self.assertEqual(plan.choices, {"rec": 1, "search": 1, "ads": 0})
        _plan_is_consistent(self, plan, entries, components, 5, 100)

    def test_both_resources_binding(self) -> None:
        entries, components = self._scenario()
        plan = solve(entries, components, cpu_capacity=6, memory_capacity=6)
        self.assertLessEqual(plan.total_cpu, 6)
        self.assertLessEqual(plan.total_memory, 6)
        self.assertEqual(plan.choices, {"rec": 1, "search": 1, "ads": 0})
        self.assertEqual(plan.total_memory, 5)
        _plan_is_consistent(self, plan, entries, components, 6, 6)


class TestInputValidation(unittest.TestCase):
    def test_unknown_tier_component_rejected(self) -> None:
        entries = [Entry("a", (Tier(1, 0, 0, ("ghost",)),))]
        with self.assertRaises(InputError):
            solve(entries, [], cpu_capacity=1, memory_capacity=1)

    def test_unknown_dependency_rejected(self) -> None:
        comps = [Component("a", 1, 1, ("ghost",))]
        with self.assertRaises(InputError):
            solve([], comps, cpu_capacity=1, memory_capacity=1)

    def test_dependency_cycle_rejected(self) -> None:
        comps = [
            Component("a", 1, 1, ("b",)),
            Component("b", 1, 1, ("a",)),
        ]
        with self.assertRaises(InputError):
            solve([], comps, cpu_capacity=10, memory_capacity=10)

    def test_self_dependency_rejected(self) -> None:
        comps = [Component("a", 1, 1, ("a",))]
        with self.assertRaises(InputError):
            solve([], comps, cpu_capacity=10, memory_capacity=10)

    def test_three_cycle_rejected(self) -> None:
        comps = [
            Component("a", 1, 1, ("b",)),
            Component("b", 1, 1, ("c",)),
            Component("c", 1, 1, ("a",)),
        ]
        with self.assertRaises(InputError):
            solve([], comps, cpu_capacity=10, memory_capacity=10)

    def test_duplicate_component_names_rejected(self) -> None:
        comps = [Component("a", 1, 1), Component("a", 2, 2)]
        with self.assertRaises(InputError):
            solve([], comps, cpu_capacity=10, memory_capacity=10)

    def test_duplicate_entry_names_rejected(self) -> None:
        entries = [Entry("a", (Tier(1, 0, 0),)), Entry("a", (Tier(2, 0, 0),))]
        with self.assertRaises(InputError):
            solve(entries, cpu_capacity=10, memory_capacity=10)

    def test_negative_cost_rejected(self) -> None:
        with self.assertRaises(InputError):
            Tier(1, -1, 0)
        with self.assertRaises(InputError):
            Component("a", 0, -1)

    def test_non_integer_cost_rejected(self) -> None:
        with self.assertRaises(InputError):
            Tier(1, 1.5, 0)
        with self.assertRaises(InputError):
            Tier(1, "2", 0)

    def test_bool_cost_rejected(self) -> None:
        with self.assertRaises(InputError):
            Tier(True, 1, 0)
        with self.assertRaises(InputError):
            Component("a", True, 0)

    def test_negative_capacity_rejected(self) -> None:
        with self.assertRaises(InputError):
            solve([], cpu_capacity=-1, memory_capacity=0)
        with self.assertRaises(InputError):
            solve([], cpu_capacity=0, memory_capacity=-1)

    def test_floor_out_of_range_rejected(self) -> None:
        with self.assertRaises(InputError):
            Entry("a", (Tier(1, 0, 0), Tier(0, 0, 0)), floor=2)
        with self.assertRaises(InputError):
            Entry("a", (Tier(1, 0, 0),), floor=-1)

    def test_entry_without_tiers_rejected(self) -> None:
        with self.assertRaises(InputError):
            Entry("a", ())

    def test_empty_names_rejected(self) -> None:
        with self.assertRaises(InputError):
            Entry("", (Tier(1, 0, 0),))
        with self.assertRaises(InputError):
            Component("", 1, 1)

    def test_tier_components_may_be_a_single_string(self) -> None:
        tier = Tier(1, 0, 0, "vec")
        self.assertEqual(tier.components, ("vec",))

    def test_duplicate_component_requirements_are_deduplicated(self) -> None:
        tier = Tier(1, 0, 0, ("vec", "vec"))
        self.assertEqual(tier.components, ("vec",))

    def test_mapping_key_mismatch_rejected(self) -> None:
        with self.assertRaises(InputError):
            solve({"a": Entry("b", (Tier(1, 0, 0),))},
                  cpu_capacity=1, memory_capacity=1)
        with self.assertRaises(InputError):
            solve([], {"a": Component("b", 1, 1)},
                  cpu_capacity=1, memory_capacity=1)


class TestMappingCoercion(unittest.TestCase):
    def test_tiers_and_entries_can_be_plain_mappings(self) -> None:
        entries = {
            "rec": {
                "floor": 1,
                "tiers": [
                    {"benefit": 100, "cpu": 4, "memory": 4, "components": ["vec"]},
                    {"benefit": 60, "cpu": 1, "memory": 1},
                    {"benefit": 10, "cpu": 0, "memory": 0},
                ],
            },
            "search": [
                {"benefit": 90, "cpu": 3, "memory": 3, "components": "vec"},
                {"benefit": 50, "cpu": 1, "memory": 1},
            ],
        }
        components = {"vec": {"cpu": 6, "memory": 6, "requires": []}}
        plan = solve(entries, components, cpu_capacity=100, memory_capacity=100)
        self.assertEqual(plan.choices, {"rec": 0, "search": 0})
        self.assertEqual(plan.total_benefit, 190)
        self.assertEqual(plan.enabled_components, frozenset({"vec"}))

    def test_component_dependencies_key_alias(self) -> None:
        components = {
            "base": {"cpu": 1, "memory": 1},
            "top": {"cpu": 1, "memory": 1, "dependencies": ["base"]},
        }
        entries = [{"name": "e", "tiers": [{"benefit": 5, "cpu": 0, "memory": 0,
                                             "components": ["top"]}]}]
        plan = solve(entries, components, cpu_capacity=2, memory_capacity=2)
        self.assertEqual(plan.enabled_components, frozenset({"top", "base"}))
        self.assertEqual(plan.component_cpu, 2)


class TestRandomisedCrossCheck(unittest.TestCase):
    def _random_case(self, rng: random.Random):
        component_count = rng.randint(0, 5)
        components = []
        for index in range(component_count):
            requires = tuple(
                f"c{j}" for j in range(index) if rng.random() < 0.35
            )
            components.append(
                Component(
                    f"c{index}",
                    rng.randint(0, 6),
                    rng.randint(0, 6),
                    requires,
                )
            )

        entry_count = rng.randint(0, 5)
        entries = []
        for index in range(entry_count):
            tier_count = rng.randint(1, 3)
            tiers = []
            for _ in range(tier_count):
                requires = tuple(
                    f"c{j}"
                    for j in range(component_count)
                    if rng.random() < 0.25
                )
                tiers.append(
                    Tier(
                        rng.randint(-5, 50),
                        rng.randint(0, 6),
                        rng.randint(0, 6),
                        requires,
                    )
                )
            floor = rng.randint(0, tier_count - 1)
            entries.append(Entry(f"e{index}", tuple(tiers), floor=floor))

        return (
            entries,
            components,
            rng.randint(0, 15),
            rng.randint(0, 15),
        )

    def test_matches_reference_on_random_inputs(self) -> None:
        rng = random.Random(20261122)
        for case_index in range(250):
            entries, components, cpu_capacity, memory_capacity = self._random_case(rng)
            reference = _reference_solve(
                entries, components, cpu_capacity, memory_capacity
            )
            with self.subTest(case=case_index):
                if reference is None:
                    with self.assertRaises(InfeasiblePlanError):
                        solve(
                            entries,
                            components,
                            cpu_capacity=cpu_capacity,
                            memory_capacity=memory_capacity,
                        )
                    continue
                plan = solve(
                    entries,
                    components,
                    cpu_capacity=cpu_capacity,
                    memory_capacity=memory_capacity,
                )
                self.assertEqual(plan.total_benefit, reference[0])
                self.assertEqual(plan.total_cpu, reference[1])
                self.assertEqual(plan.total_memory, reference[2])
                self.assertEqual(
                    tuple(selection.tier_index for selection in plan.selections),
                    reference[3],
                )
                _plan_is_consistent(
                    self, plan, entries, components, cpu_capacity, memory_capacity
                )

    def test_objective_value_is_independent_of_input_order(self) -> None:
        rng = random.Random(7)
        for _ in range(80):
            entries, components, cpu_capacity, memory_capacity = self._random_case(rng)
            reference = _reference_solve(
                entries, components, cpu_capacity, memory_capacity
            )
            shuffled = list(entries)
            rng.shuffle(shuffled)
            if reference is None:
                with self.assertRaises(InfeasiblePlanError):
                    solve(
                        shuffled,
                        components,
                        cpu_capacity=cpu_capacity,
                        memory_capacity=memory_capacity,
                    )
                continue
            plan = solve(
                shuffled,
                components,
                cpu_capacity=cpu_capacity,
                memory_capacity=memory_capacity,
            )
            self.assertEqual(plan.total_benefit, reference[0])
            self.assertEqual(plan.total_cpu, reference[1])
            self.assertEqual(plan.total_memory, reference[2])
            _plan_is_consistent(
                self, plan, shuffled, components, cpu_capacity, memory_capacity
            )
if __name__ == "__main__":
    unittest.main()
