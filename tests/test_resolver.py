"""Acceptance tests for the cross-compilation dependency solver.

Run from the repository root with::

    python3 -m unittest discover -s tests -v

Every test drives the public API (``resolver.resolve`` and the model
constructors) and asserts observable behaviour; no test inspects private
state.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import resolver
from resolver import (
    DependencyKind,
    Domain,
    Interval,
    VersionRange,
    build,
    compare_versions,
    locked,
    pkg,
    render_chain,
    request,
    resolve,
    root,
    run,
    vrange,
)


def v(*parts: int):
    return tuple(parts)


class SolverTestCase(unittest.TestCase):
    def assert_selected(self, result, domain, name, version):
        self.assertTrue(result.ok, msg=result.failure.render() if result.failure else "")
        selection = result.selection_for(domain, name)
        self.assertIsNotNone(selection, f"expected {domain}:{name} to be selected")
        self.assertEqual(selection.version, v(*version), msg=render_result(result))

    def assert_not_selected(self, result, domain, name):
        self.assertIsNone(result.selection_for(domain, name))

    def assert_failed(self, result):
        self.assertFalse(result.ok, msg="expected the request to be unsatisfiable")
        self.assertEqual(result.selections, ())
        self.assertIsNotNone(result.failure)

    def assert_one_version_per_slot(self, result):
        seen = {}
        for selection in result.selections:
            slot = (selection.domain, selection.name)
            self.assertNotIn(slot, seen, f"slot {slot} selected more than once")
            seen[slot] = selection.version

    def assert_chains_start_at_root(self, report):
        self.assertTrue(report.chains, "a conflict report must carry at least one chain")
        for chain in report.chains:
            self.assertTrue(chain, "chain must not be empty")
            self.assertIsNone(
                chain[0].package,
                "the first hop of a conflict chain must be a root requirement",
            )
            self.assertIsNone(chain[0].kind)


def render_result(result):
    if result.failure is not None:
        return result.failure.render()
    return ", ".join(
        f"{s.domain}:{s.name}@{'.'.join(map(str, s.version))}" for s in result.selections
    )


# ---------------------------------------------------------------------------
# Version primitives
# ---------------------------------------------------------------------------


class VersionPrimitiveTests(unittest.TestCase):
    def test_lexicographic_version_order(self):
        self.assertEqual(compare_versions(v(1, 2), v(1, 10)), -1)
        self.assertEqual(compare_versions(v(2, 0), v(1, 99)), 1)
        self.assertEqual(compare_versions(v(1, 0), v(1, 0)), 0)
        # A shorter vector sorts before the longer vector that extends it.
        self.assertEqual(compare_versions(v(1, 0), v(1, 0, 0)), -1)

    def test_interval_intersection_and_emptiness(self):
        merged = Interval(1, 5).intersect(Interval(3, 9))
        self.assertEqual(merged, Interval(3, 5))
        self.assertIsNone(Interval(1, 2).intersect(Interval(3, 4)))
        self.assertIsNone(Interval(None, 2).intersect(Interval(3, None)))
        self.assertTrue(Interval(2, 4).contains(3))
        self.assertFalse(Interval(2, 4).contains(5))

    def test_version_range_intersection(self):
        left = vrange({0: (1, 1), 1: (0, 9)})
        right = vrange({0: (1, 2)})
        merged = left.intersect(right)
        self.assertIsNotNone(merged)
        self.assertTrue(merged.allows(v(1, 9)))
        self.assertFalse(merged.allows(v(2, 0)))
        self.assertIsNone(left.intersect(vrange({0: (2, 3)})))

    def test_empty_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            Interval(5, 1)


# ---------------------------------------------------------------------------
# Domain placement: build -> host, run -> referrer
# ---------------------------------------------------------------------------


class DomainPlacementTests(SolverTestCase):
    def test_build_dependency_of_target_root_lands_in_host(self):
        req = request(
            roots=[root("gen", Domain.TARGET)],
            candidates=[
                pkg("gen", (1, 0), [build("tool")]),
                pkg("tool", (1, 0)),
            ],
            host_platform="x86_64",
            target_platform="arm64",
        )
        result = resolve(req)
        self.assert_selected(result, Domain.HOST, "tool", (1, 0))
        self.assert_not_selected(result, Domain.TARGET, "tool")
        self.assert_selected(result, Domain.TARGET, "gen", (1, 0))
        self.assert_one_version_per_slot(result)

    def test_run_dependency_follows_host_referrer(self):
        req = request(
            roots=[root("worker", Domain.HOST)],
            candidates=[
                pkg("worker", (1, 0), [run("zlib")]),
                pkg("zlib", (1, 0)),
            ],
            host_platform="x86_64",
            target_platform="arm64",
        )
        result = resolve(req)
        self.assert_selected(result, Domain.HOST, "zlib", (1, 0))
        self.assert_not_selected(result, Domain.TARGET, "zlib")

    def test_run_dependency_follows_target_referrer(self):
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[
                pkg("app", (1, 0), [run("zlib")]),
                pkg("zlib", (1, 0)),
            ],
            host_platform="x86_64",
            target_platform="arm64",
        )
        result = resolve(req)
        self.assert_selected(result, Domain.TARGET, "zlib", (1, 0))
        self.assert_not_selected(result, Domain.HOST, "zlib")

    def test_host_and_target_referrers_do_not_share_slots(self):
        req = request(
            roots=[root("worker", Domain.HOST), root("app", Domain.TARGET)],
            candidates=[
                pkg("worker", (1, 0), [run("zlib", vrange({0: (1, 1)}))]),
                pkg("app", (1, 0), [run("zlib", vrange({0: (2, 2)}))]),
                pkg("zlib", (1, 0)),
                pkg("zlib", (2, 0)),
            ],
            host_platform="x86_64",
            target_platform="arm64",
        )
        result = resolve(req)
        self.assert_selected(result, Domain.HOST, "zlib", (1, 0))
        self.assert_selected(result, Domain.TARGET, "zlib", (2, 0))

    def test_transitive_chain_stays_in_the_domain_it_started_in(self):
        """target app -> build codegen -> run parser keeps ``parser`` in host."""
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[
                pkg("app", (1, 0), [build("codegen")]),
                pkg("codegen", (1, 0), [run("parser")]),
                pkg("parser", (1, 0), [build("lexer")]),
                pkg("lexer", (1, 0)),
            ],
            host_platform="x86_64",
            target_platform="arm64",
        )
        result = resolve(req)
        self.assert_selected(result, Domain.HOST, "codegen", (1, 0))
        self.assert_selected(result, Domain.HOST, "parser", (1, 0))
        self.assert_selected(result, Domain.HOST, "lexer", (1, 0))
        self.assert_not_selected(result, Domain.TARGET, "parser")
        self.assert_not_selected(result, Domain.TARGET, "lexer")

    def test_build_dependency_of_a_host_package_stays_in_host(self):
        req = request(
            roots=[root("tool", Domain.HOST)],
            candidates=[
                pkg("tool", (1, 0), [build("bootstrap")]),
                pkg("bootstrap", (1, 0)),
            ],
            host_platform="x86_64",
            target_platform="arm64",
        )
        result = resolve(req)
        self.assert_selected(result, Domain.HOST, "bootstrap", (1, 0))
        self.assert_not_selected(result, Domain.TARGET, "bootstrap")


# ---------------------------------------------------------------------------
# Cross constraint: host generator and target runtime may need different
# major versions of the same package, and both must assemble.
# ---------------------------------------------------------------------------


class CrossCompilationTests(SolverTestCase):
    def test_generator_and_runtime_use_different_majors_of_same_package(self):
        """Host codegen needs libssl 1.x while the target image needs libssl 3.x."""
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[
                pkg(
                    "app",
                    (1, 0),
                    [
                        build("codegen"),
                        run("libssl", vrange({0: (3, 3)})),
                    ],
                ),
                pkg("codegen", (1, 0), [build("libssl", vrange({0: (1, 1)}))]),
                pkg("libssl", (1, 9)),
                pkg("libssl", (3, 2)),
            ],
            host_platform="x86_64",
            target_platform="arm64",
        )
        result = resolve(req)
        self.assertTrue(result.ok, msg=render_result(result))
        self.assert_selected(result, Domain.HOST, "libssl", (1, 9))
        self.assert_selected(result, Domain.TARGET, "libssl", (3, 2))
        self.assert_selected(result, Domain.HOST, "codegen", (1, 0))
        self.assert_selected(result, Domain.TARGET, "app", (1, 0))
        host_version = result.selection_for(Domain.HOST, "libssl").version
        target_version = result.selection_for(Domain.TARGET, "libssl").version
        self.assertNotEqual(host_version[0], target_version[0])
        self.assert_one_version_per_slot(result)

    def test_same_root_name_in_both_domains_resolves_independently(self):
        req = request(
            roots=[
                root("libssl", Domain.HOST, vrange({0: (1, 1)})),
                root("libssl", Domain.TARGET, vrange({0: (3, 3)})),
            ],
            candidates=[pkg("libssl", (1, 9)), pkg("libssl", (3, 2))],
            host_platform="x86_64",
            target_platform="arm64",
        )
        result = resolve(req)
        self.assert_selected(result, Domain.HOST, "libssl", (1, 9))
        self.assert_selected(result, Domain.TARGET, "libssl", (3, 2))


# ---------------------------------------------------------------------------
# One version per slot, reachability
# ---------------------------------------------------------------------------


class SlotAndReachabilityTests(SolverTestCase):
    def test_two_constraints_force_a_single_middle_version(self):
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[
                pkg("app", (1, 0), [run("b", vrange({0: (1, None)})), run("c")]),
                pkg("c", (1, 0), [run("b", vrange({0: (None, 1), 1: (None, 5)}))]),
                pkg("b", (1, 0)),
                pkg("b", (1, 5)),
                pkg("b", (2, 0)),
            ],
            host_platform="x86_64",
            target_platform="arm64",
        )
        result = resolve(req)
        self.assert_selected(result, Domain.TARGET, "b", (1, 5))
        slots = [s for s in result.selections if s.name == "b"]
        self.assertEqual(len(slots), 1)
        self.assert_one_version_per_slot(result)

    def test_unreachable_candidates_are_not_selected(self):
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[
                pkg("app", (1, 0)),
                pkg("unused", (9, 9)),
                pkg("also-unused", (1, 0), [run("deep")]),
                pkg("deep", (1, 0)),
            ],
            host_platform="x86_64",
            target_platform="arm64",
        )
        result = resolve(req)
        self.assertEqual(
            [(s.domain, s.name) for s in result.selections],
            [(Domain.TARGET, "app")],
        )


# ---------------------------------------------------------------------------
# Backtracking and constraint-driven reselection
# ---------------------------------------------------------------------------


class BacktrackingTests(SolverTestCase):
    def test_new_constraint_forces_reselection(self):
        """The greatest lib version is retracted once ``extra`` arrives."""
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[
                pkg(
                    "app",
                    (2, 0),
                    [run("lib", vrange({0: (1, None)})), run("extra")],
                ),
                pkg("extra", (1, 0), [run("lib", vrange({0: (None, 1), 1: (None, 5)}))]),
                pkg("lib", (1, 0)),
                pkg("lib", (1, 5)),
                pkg("lib", (2, 0)),
            ],
            host_platform="x86_64",
            target_platform="arm64",
        )
        result = resolve(req)
        self.assert_selected(result, Domain.TARGET, "app", (2, 0))
        self.assert_selected(result, Domain.TARGET, "extra", (1, 0))
        # lib@2.0 satisfied app alone, but extra caps lib at 1.5.
        self.assert_selected(result, Domain.TARGET, "lib", (1, 5))

    def test_locked_version_is_retracted_when_it_cannot_satisfy(self):
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[
                # app@2 is the locked choice, but it demands lib>=3 which no
                # candidate provides; the solver must backtrack to app@1.
                pkg("app", (2, 0), [run("lib", vrange({0: (3, None)}))]),
                pkg("app", (1, 0), [run("lib", vrange({0: (1, 1)}))]),
                pkg("lib", (1, 0)),
                pkg("lib", (2, 0)),
            ],
            host_platform="x86_64",
            target_platform="arm64",
            lock=[locked(Domain.TARGET, "app", (2, 0))],
        )
        result = resolve(req)
        self.assertTrue(result.ok, msg=render_result(result))
        self.assert_selected(result, Domain.TARGET, "app", (1, 0))
        self.assert_selected(result, Domain.TARGET, "lib", (1, 0))


# ---------------------------------------------------------------------------
# Objective: minimise re-versioned locked slots, then deterministic tie-break
# ---------------------------------------------------------------------------


class ObjectiveTests(SolverTestCase):
    def test_greatest_version_wins_without_a_lock(self):
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[pkg("app", (1, 0)), pkg("app", (2, 0))],
            host_platform="x86_64",
            target_platform="arm64",
        )
        self.assert_selected(resolve(req), Domain.TARGET, "app", (2, 0))

    def test_lock_prefers_an_unchanged_slot_over_a_greater_version(self):
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[pkg("app", (1, 0)), pkg("app", (2, 0))],
            host_platform="x86_64",
            target_platform="arm64",
            lock=[locked(Domain.TARGET, "app", (1, 0))],
        )
        result = resolve(req)
        self.assert_selected(result, Domain.TARGET, "app", (1, 0))
        self.assertEqual(result.changed_locked_slots, 0)

    def test_objective_counts_every_overlapping_reversioned_slot(self):
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[
                pkg("app", (1, 0), [run("lib")]),
                pkg("app", (2, 0), [run("lib")]),
                pkg("lib", (1, 0)),
                pkg("lib", (2, 0)),
            ],
            host_platform="x86_64",
            target_platform="arm64",
            lock=[
                locked(Domain.TARGET, "app", (2, 0)),
                locked(Domain.TARGET, "lib", (1, 0)),
            ],
        )
        result = resolve(req)
        # (app 2, lib 1) changes nothing; (app 2, lib 2) would change one slot
        # even though lib@2 is the greater version.
        self.assert_selected(result, Domain.TARGET, "app", (2, 0))
        self.assert_selected(result, Domain.TARGET, "lib", (1, 0))
        self.assertEqual(result.changed_locked_slots, 0)

    def test_locked_slot_that_is_not_reachable_is_free(self):
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[pkg("app", (1, 0)), pkg("app", (2, 0))],
            host_platform="x86_64",
            target_platform="arm64",
            lock=[
                locked(Domain.TARGET, "app", (1, 0)),
                locked(Domain.TARGET, "ghost", (7, 7)),
            ],
        )
        result = resolve(req)
        self.assert_selected(result, Domain.TARGET, "app", (1, 0))
        self.assert_not_selected(result, Domain.TARGET, "ghost")
        self.assertEqual(result.changed_locked_slots, 0)

    def test_lock_is_a_preference_not_a_hard_constraint(self):
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[pkg("app", (1, 0)), pkg("app", (2, 0))],
            host_platform="x86_64",
            target_platform="arm64",
            lock=[locked(Domain.TARGET, "app", (9, 0))],
        )
        result = resolve(req)
        self.assert_selected(result, Domain.TARGET, "app", (2, 0))
        # The slot overlaps the lock and was re-versioned; the request still
        # succeeds because the lock never constrains feasibility.
        self.assertEqual(result.changed_locked_slots, 1)

    def test_tiebreak_uses_fixed_slot_order_host_before_target(self):
        """Host slot ``lib`` decides before the target slot ``app``.

        Negotiated trade-off: choosing app@2 (greater target version) forces
        worker@2 in host, which drops ``lib`` from the host domain.  Choosing
        app@1 keeps host:lib selected.  Because host slots are compared first
        and an unselected slot ranks below any version, app@1 wins.
        """
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[
                pkg("app", (2, 0), [build("worker", vrange({0: (2, 2)}))]),
                pkg("app", (1, 0), [build("worker", vrange({0: (1, 1)}))]),
                pkg("worker", (2, 0)),
                pkg("worker", (1, 0), [build("lib", vrange({0: (1, 1)}))]),
                pkg("lib", (1, 0)),
            ],
            host_platform="x86_64",
            target_platform="arm64",
        )
        result = resolve(req)
        self.assert_selected(result, Domain.TARGET, "app", (1, 0))
        self.assert_selected(result, Domain.HOST, "worker", (1, 0))
        self.assert_selected(result, Domain.HOST, "lib", (1, 0))

    def test_resolution_is_deterministic(self):
        def make():
            return request(
                roots=[root("app", Domain.TARGET)],
                candidates=[
                    pkg("app", (2, 0), [run("lib", vrange({0: (1, None)})), run("extra")]),
                    pkg("app", (1, 0)),
                    pkg("extra", (1, 0), [run("lib", vrange({0: (None, 1), 1: (None, 5)}))]),
                    pkg("lib", (1, 0)),
                    pkg("lib", (1, 5)),
                    pkg("lib", (2, 0)),
                ],
                host_platform="x86_64",
                target_platform="arm64",
            )

        first = resolve(make())
        second = resolve(make())
        self.assertEqual(first.selections, second.selections)
        self.assertEqual(first.changed_locked_slots, second.changed_locked_slots)


# ---------------------------------------------------------------------------
# Platform gating
# ---------------------------------------------------------------------------


class PlatformTests(SolverTestCase):
    def test_candidate_must_run_on_the_domain_platform(self):
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[
                pkg("app", (1, 0), [run("lib")]),
                pkg("lib", (2, 0), platforms=["x86_64"]),  # greatest, wrong target
                pkg("lib", (1, 0), platforms=["arm64"]),
            ],
            host_platform="x86_64",
            target_platform="arm64",
        )
        result = resolve(req)
        self.assert_selected(result, Domain.TARGET, "lib", (1, 0))

    def test_host_domain_is_judged_against_the_host_platform(self):
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[
                pkg("app", (1, 0), [build("tool")]),
                pkg("tool", (5, 0), platforms=["arm64"]),  # greatest, wrong host
                pkg("tool", (4, 0), platforms=["x86_64"]),
            ],
            host_platform="x86_64",
            target_platform="arm64",
        )
        result = resolve(req)
        self.assert_selected(result, Domain.HOST, "tool", (4, 0))

    def test_dependency_edge_is_gated_by_destination_platform(self):
        candidates = [
            pkg("app", (1, 0), [run("opt", platforms=["linux"])]),
            pkg("opt", (1, 0)),
        ]
        on_linux = resolve(
            request(
                roots=[root("app", Domain.TARGET)],
                candidates=candidates,
                host_platform="x86_64",
                target_platform="linux",
            )
        )
        self.assert_selected(on_linux, Domain.TARGET, "opt", (1, 0))

        on_arm64 = resolve(
            request(
                roots=[root("app", Domain.TARGET)],
                candidates=candidates,
                host_platform="x86_64",
                target_platform="arm64",
            )
        )
        self.assert_not_selected(on_arm64, Domain.TARGET, "opt")

    def test_no_runnable_candidate_is_a_conflict(self):
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[
                pkg("app", (1, 0), [run("lib")]),
                pkg("lib", (1, 0), platforms=["x86_64"]),
            ],
            host_platform="x86_64",
            target_platform="arm64",
        )
        result = resolve(req)
        self.assert_failed(result)
        self.assertEqual(result.failure.domain, Domain.TARGET)
        self.assertEqual(result.failure.package, "lib")
        self.assertIn("arm64", result.failure.reason)
        self.assert_chains_start_at_root(result.failure)


# ---------------------------------------------------------------------------
# Unsatisfiable requests: chains trace back to the contradictory constraints
# ---------------------------------------------------------------------------


class ConflictReportTests(SolverTestCase):
    def test_missing_candidate_is_explained_by_a_root_chain(self):
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[
                pkg("app", (1, 0), [run("lib", vrange({0: (2, 2)}))]),
                pkg("lib", (1, 0)),
            ],
            host_platform="x86_64",
            target_platform="arm64",
        )
        result = resolve(req)
        self.assert_failed(result)
        report = result.failure
        self.assertEqual(report.package, "lib")
        self.assert_chains_start_at_root(report)
        chain = report.chains[0]
        # The chain must end on the contradictory constraint, not on a root.
        self.assertEqual(chain[-1].target, "lib")
        self.assertFalse(chain[-1].constraint.allows(v(1, 0)))
        self.assertTrue(chain[-1].constraint.allows(v(2, 0)))
        rendered = render_chain(chain)
        self.assertIn("app", rendered)
        self.assertIn("lib", rendered)

    def test_two_conflicting_constraints_both_appear_in_the_report(self):
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[
                pkg(
                    "app",
                    (1, 0),
                    [run("lib", vrange({0: (2, None)})), run("plugin")],
                ),
                pkg("plugin", (1, 0), [run("lib", vrange({0: (None, 1), 1: (None, 5)}))]),
                pkg("lib", (1, 0)),
                pkg("lib", (1, 5)),
                pkg("lib", (2, 0)),
            ],
            host_platform="x86_64",
            target_platform="arm64",
        )
        result = resolve(req)
        self.assert_failed(result)
        report = result.failure
        self.assertEqual(report.package, "lib")
        self.assertGreaterEqual(len(report.chains), 2)
        self.assert_chains_start_at_root(report)

        endpoints = []
        for chain in report.chains:
            last = chain[-1]
            self.assertEqual(last.target, "lib")
            self.assertEqual(last.target_domain, Domain.TARGET)
            endpoints.append(last.constraint)
        # One constraint insists on >= 2, the other caps at <= 1.5.
        self.assertTrue(any(c.allows(v(2, 0)) and not c.allows(v(1, 5)) for c in endpoints))
        self.assertTrue(any(c.allows(v(1, 5)) and not c.allows(v(2, 0)) for c in endpoints))

    def test_conflict_inside_host_domain_is_reported_in_host(self):
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[
                pkg("app", (1, 0), [build("tool", vrange({0: (2, None)}))]),
                pkg("tool", (1, 0)),
            ],
            host_platform="x86_64",
            target_platform="arm64",
        )
        result = resolve(req)
        self.assert_failed(result)
        self.assertEqual(result.failure.domain, Domain.HOST)
        self.assertEqual(result.failure.package, "tool")
        self.assert_chains_start_at_root(result.failure)

    def test_contradicting_root_requirements_are_reported(self):
        req = request(
            roots=[
                root("lib", Domain.TARGET, vrange({0: (1, 1)})),
                root("lib", Domain.TARGET, vrange({0: (2, 2)})),
            ],
            candidates=[pkg("lib", (1, 0)), pkg("lib", (2, 0))],
            host_platform="x86_64",
            target_platform="arm64",
        )
        result = resolve(req)
        self.assert_failed(result)
        report = result.failure
        self.assertEqual(report.domain, Domain.TARGET)
        self.assertEqual(report.package, "lib")
        self.assertGreaterEqual(len(report.chains), 2)
        self.assert_chains_start_at_root(report)
        for chain in report.chains:
            self.assertEqual(len(chain), 1, "a root-only conflict has one hop per chain")

    def test_empty_root_set_produces_an_empty_assembly(self):
        result = resolve(
            request(
                roots=[],
                candidates=[pkg("app", (1, 0))],
                host_platform="x86_64",
                target_platform="arm64",
            )
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.selections, ())
        self.assertEqual(result.changed_locked_slots, 0)


# ---------------------------------------------------------------------------
# Input validation and budgets
# ---------------------------------------------------------------------------


class ValidationTests(SolverTestCase):
    def test_duplicate_candidates_are_rejected(self):
        with self.assertRaises(ValueError):
            request(
                roots=[root("app", Domain.TARGET)],
                candidates=[pkg("app", (1, 0)), pkg("app", (1, 0))],
                host_platform="x86_64",
                target_platform="arm64",
            )

    def test_conflicting_lock_entries_are_rejected(self):
        with self.assertRaises(ValueError):
            request(
                roots=[root("app", Domain.TARGET)],
                candidates=[pkg("app", (1, 0))],
                host_platform="x86_64",
                target_platform="arm64",
                lock=[
                    locked(Domain.TARGET, "app", (1, 0)),
                    locked(Domain.TARGET, "app", (2, 0)),
                ],
            )

    def test_unknown_domain_is_rejected(self):
        with self.assertRaises(ValueError):
            root("app", "sidecar")

    def test_unknown_dependency_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            resolver.Dependency(name="lib", kind="link")

    def test_negative_version_component_is_rejected(self):
        with self.assertRaises(ValueError):
            pkg("app", (-1, 0))

    def test_node_budget_is_enforced(self):
        req = request(
            roots=[root("app", Domain.TARGET)],
            candidates=[
                pkg("app", (1, 0), [run("lib", vrange({0: (1, None)}))]),
                pkg("lib", (1, 0)),
                pkg("lib", (2, 0)),
            ],
            host_platform="x86_64",
            target_platform="arm64",
        )
        with self.assertRaises(resolver.ResolutionLimitError):
            resolve(req, max_nodes=1)


if __name__ == "__main__":
    unittest.main()
