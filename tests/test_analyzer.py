"""Analyzer unit tests.

Each test carries a stable case id (CM-xx) mapping one requirement of the
coverage-matrix brief to concrete input / expected output:

  CM-01  entries may share modules; a shared checkpoint is reachable from both
  CM-02  the call graph may contain cycles; traversal still terminates
  CM-03  one test covering several checkpoints is recorded per checkpoint
  CM-04  a trace from a different build does not count as coverage
  CM-05  a trace naming an unknown entry is globally unattributable
  CM-06  a trace naming an unknown checkpoint is unattributable
  CM-07  a trace claiming a checkpoint unreachable from its entry is rejected
  CM-08  entry with reachable checkpoints but no traces -> statically_reachable
  CM-09  entry with a valid matching trace -> observed_covered
  CM-10  entry with no reachable checkpoints -> no_reachable_checkpoint
  CM-11  nearest observed checkpoint is chosen by BFS distance with path
  CM-12  uncovered_entries lists statically_reachable + unattributable only
"""

import unittest

from covmatrix.analyzer import (
    NO_REACHABLE_CHECKPOINT,
    OBSERVED_COVERED,
    STATICALLY_REACHABLE,
    UNATTRIBUTABLE,
    analyze,
    bfs_distances,
    reachable_modules,
)
from covmatrix.model import CallGraph, Checkpoint, Entry, Trace

BUILD = "B1"


def make_graph(edges):
    adjacency = {}
    modules = set()
    for caller, callee in edges:
        adjacency.setdefault(caller, set()).add(callee)
        adjacency.setdefault(callee, adjacency.get(callee, set()))
        modules.update((caller, callee))
    return CallGraph(
        modules=modules,
        adjacency={m: sorted(t) for m, t in adjacency.items()},
    )


def entry(eid, module):
    return Entry(id=eid, name=eid + "-name", module=module)


def cp(cid, module):
    return Checkpoint(id=cid, module=module, description="")


def trace(tid, entry_id, cps, build=BUILD):
    return Trace(test_id=tid, build=build, entry_id=entry_id, checkpoints=tuple(cps))


def entry_by_id(analysis, eid):
    return next(e for e in analysis["entries"] if e["id"] == eid)


class TestReachability(unittest.TestCase):
    def test_cm01_shared_module_reachable_from_two_entries(self):
        # Input: E1->m1, E2->m2, both call shared; CP1 lives in shared.
        graph = make_graph([("m1", "shared"), ("m2", "shared")])
        entries = [entry("E1", "m1"), entry("E2", "m2")]
        result = analyze(entries, graph, [cp("CP1", "shared")], [], BUILD)
        # Expected: CP1 statically reachable from both entries.
        self.assertEqual(entry_by_id(result, "E1")["reachable_checkpoints"], ["CP1"])
        self.assertEqual(entry_by_id(result, "E2")["reachable_checkpoints"], ["CP1"])

    def test_cm02_cycle_in_call_graph_terminates(self):
        # Input: cycle a -> b -> c -> a plus exit c -> d.
        graph = make_graph([("a", "b"), ("b", "c"), ("c", "a"), ("c", "d")])
        # Expected: traversal visits every module exactly once and terminates.
        self.assertEqual(reachable_modules(graph, "a"), {"a", "b", "c", "d"})
        dist, _ = bfs_distances(graph, "a")
        self.assertEqual(dist, {"a": 0, "b": 1, "c": 2, "d": 3})


class TestTraceAttribution(unittest.TestCase):
    def setUp(self):
        self.graph = make_graph([("m1", "ma"), ("ma", "mb")])
        self.entries = [entry("E1", "m1")]
        self.checkpoints = [cp("CP1", "ma"), cp("CP2", "mb")]

    def test_cm03_one_test_multiple_checkpoints_recorded_separately(self):
        # Input: single trace T1 hitting CP1 and CP2.
        result = analyze(self.entries, self.graph, self.checkpoints,
                         [trace("T1", "E1", ["CP1", "CP2"])], BUILD)
        records = entry_by_id(result, "E1")["coverage_records"]
        # Expected: two separate coverage records, one per checkpoint.
        self.assertEqual(
            records,
            [
                {"test_id": "T1", "checkpoint_id": "CP1"},
                {"test_id": "T1", "checkpoint_id": "CP2"},
            ],
        )

    def test_cm04_build_mismatch_is_not_coverage(self):
        # Input: trace recorded against old build B0.
        result = analyze(self.entries, self.graph, self.checkpoints,
                         [trace("T1", "E1", ["CP1"], build="B0")], BUILD)
        e1 = entry_by_id(result, "E1")
        # Expected: no coverage; record rejected with reason build_mismatch.
        self.assertEqual(e1["coverage_records"], [])
        self.assertEqual(e1["status"], UNATTRIBUTABLE)
        self.assertEqual(e1["invalid_traces"][0]["reason"], "build_mismatch")

    def test_cm05_unknown_entry_is_globally_unattributable(self):
        # Input: trace naming entry E99 which is not in the entry list.
        result = analyze(self.entries, self.graph, self.checkpoints,
                         [trace("T1", "E99", ["CP1"])], BUILD)
        # Expected: reported in the global unattributable list, not on E1.
        self.assertEqual(len(result["unattributable_traces"]), 1)
        self.assertEqual(result["unattributable_traces"][0]["reason"], "unknown_entry")
        self.assertEqual(entry_by_id(result, "E1")["invalid_traces"], [])

    def test_cm06_unknown_checkpoint_is_unattributable(self):
        # Input: trace naming CP9 which the patch did not touch.
        result = analyze(self.entries, self.graph, self.checkpoints,
                         [trace("T1", "E1", ["CP9"])], BUILD)
        e1 = entry_by_id(result, "E1")
        # Expected: rejected with reason unknown_checkpoint, no coverage.
        self.assertEqual(e1["coverage_records"], [])
        self.assertEqual(e1["invalid_traces"][0]["reason"], "unknown_checkpoint")

    def test_cm07_checkpoint_not_reachable_from_entry_is_rejected(self):
        # Input: E2 sits in module mz with no edges; trace claims CP1 for E2.
        entries = self.entries + [entry("E2", "mz")]
        result = analyze(entries, self.graph, self.checkpoints,
                         [trace("T1", "E2", ["CP1"])], BUILD)
        e2 = entry_by_id(result, "E2")
        # Expected: rejected with reason checkpoint_not_reachable.
        self.assertEqual(e2["coverage_records"], [])
        self.assertEqual(e2["invalid_traces"][0]["reason"], "checkpoint_not_reachable")


class TestEntryStatus(unittest.TestCase):
    def test_cm08_reachable_without_traces_is_statically_reachable(self):
        graph = make_graph([("m1", "ma")])
        result = analyze([entry("E1", "m1")], graph, [cp("CP1", "ma")], [], BUILD)
        self.assertEqual(entry_by_id(result, "E1")["status"], STATICALLY_REACHABLE)

    def test_cm09_matching_trace_makes_entry_observed_covered(self):
        graph = make_graph([("m1", "ma")])
        result = analyze([entry("E1", "m1")], graph, [cp("CP1", "ma")],
                         [trace("T1", "E1", ["CP1"])], BUILD)
        e1 = entry_by_id(result, "E1")
        self.assertEqual(e1["status"], OBSERVED_COVERED)
        self.assertEqual(e1["covered_checkpoints"], ["CP1"])

    def test_cm10_no_reachable_checkpoint_status(self):
        # Input: entry module 'isolated' has no path to the checkpoint module.
        graph = make_graph([("other", "ma")])
        result = analyze([entry("E1", "isolated")], graph, [cp("CP1", "ma")], [], BUILD)
        self.assertEqual(entry_by_id(result, "E1")["status"], NO_REACHABLE_CHECKPOINT)


class TestNearestObserved(unittest.TestCase):
    def test_cm11_nearest_observed_checkpoint_by_bfs_distance(self):
        # Input: chain m1 -> ma -> mb; CP1 in ma (observed via E1), CP2 in mb
        # (observed via E1 too). E2 sits in mb and is uncovered.
        graph = make_graph([("m1", "ma"), ("ma", "mb")])
        entries = [entry("E1", "m1"), entry("E2", "mb")]
        checkpoints = [cp("CP1", "ma"), cp("CP2", "mb")]
        traces = [trace("T1", "E1", ["CP1", "CP2"])]
        result = analyze(entries, graph, checkpoints, traces, BUILD)
        # E2 has no traces of its own -> statically_reachable and uncovered.
        e2 = entry_by_id(result, "E2")
        self.assertEqual(e2["status"], STATICALLY_REACHABLE)
        # Expected: nearest observed checkpoint is CP2 in its own module.
        nearest = e2["nearest_observed_checkpoint"]
        self.assertEqual(nearest["id"], "CP2")
        self.assertEqual(nearest["distance"], 0)
        self.assertEqual(nearest["path"], ["mb"])

    def test_cm11b_nearest_observed_none_when_nothing_reachable(self):
        # Input: isolated uncovered entry; observed checkpoint elsewhere.
        graph = make_graph([("m1", "ma")])
        entries = [entry("E1", "m1"), entry("E2", "isolated")]
        checkpoints = [cp("CP1", "ma")]
        # E2 gets an unattributable trace so it lands in the uncovered set.
        traces = [trace("T1", "E1", ["CP1"]), trace("T2", "E2", ["CP1"])]
        result = analyze(entries, graph, checkpoints, traces, BUILD)
        e2 = entry_by_id(result, "E2")
        self.assertEqual(e2["status"], UNATTRIBUTABLE)
        # Expected: no observed checkpoint is reachable from 'isolated'.
        self.assertIsNone(e2["nearest_observed_checkpoint"])

    def test_cm12_uncovered_entries_membership(self):
        # Input: E1 covered, E2 reachable-but-untested, E3 with a bad trace,
        # E4 with nothing reachable.
        graph = make_graph([("m1", "ma"), ("m2", "ma")])
        entries = [
            entry("E1", "m1"),
            entry("E2", "m2"),
            entry("E3", "m1"),
            entry("E4", "isolated"),
        ]
        checkpoints = [cp("CP1", "ma")]
        traces = [
            trace("T1", "E1", ["CP1"]),
            trace("T2", "E3", ["CP1"], build="B0"),  # build mismatch
        ]
        result = analyze(entries, graph, checkpoints, traces, BUILD)
        # Expected: E2 (statically_reachable) and E3 (unattributable) are
        # uncovered; E1 (covered) and E4 (no_reachable_checkpoint) are not.
        self.assertEqual(result["uncovered_entries"], ["E2", "E3"])


if __name__ == "__main__":
    unittest.main()
