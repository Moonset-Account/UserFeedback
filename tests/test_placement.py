"""Tests for the backtracking group placement engine."""

import unittest

from group_placement import (
    MAX_NODES,
    MAX_PODS,
    Container,
    Node,
    PlacementInputError,
    Pod,
    place_group,
)


def _simple_pod(name, cpu, memory, eligible_nodes, overhead=0.0):
    """Build a Pod whose single app stage peaks at exactly (cpu, memory)."""
    return Pod(
        name=name,
        apps=(Container(name="app", cpu=cpu, memory=memory, kind="app"),),
        cpu_overhead=overhead,
        memory_overhead=overhead,
        eligible_nodes=tuple(eligible_nodes),
    )


class SuccessfulPlacementTest(unittest.TestCase):
    def test_returns_complete_assignment_and_residuals(self):
        pods = [
            _simple_pod("p1", cpu=3, memory=2, eligible_nodes=("n1", "n2")),
            _simple_pod("p2", cpu=4, memory=3, eligible_nodes=("n1",)),
        ]
        nodes = [
            Node("n1", cpu_available=5, memory_available=4),
            Node("n2", cpu_available=8, memory_available=8),
        ]

        result = place_group(pods, nodes)

        self.assertTrue(result.success)
        self.assertEqual(result.placement, {"p1": "n2", "p2": "n1"})
        self.assertEqual(
            result.residuals["n1"].cpu_available, 5 - 4
        )
        self.assertEqual(
            result.residuals["n1"].memory_available, 4 - 3
        )
        self.assertEqual(
            result.residuals["n2"].cpu_available, 8 - 3
        )
        self.assertEqual(
            result.residuals["n2"].memory_available, 8 - 2
        )

    def test_respects_eligibility_lists(self):
        pods = [
            _simple_pod("only-n1", cpu=1, memory=1, eligible_nodes=("n1",)),
            _simple_pod("only-n2", cpu=1, memory=1, eligible_nodes=("n2",)),
        ]
        nodes = [
            Node("n1", cpu_available=2, memory_available=2),
            Node("n2", cpu_available=2, memory_available=2),
        ]

        result = place_group(pods, nodes)

        self.assertTrue(result.success)
        self.assertEqual(result.placement["only-n1"], "n1")
        self.assertEqual(result.placement["only-n2"], "n2")

    def test_pods_share_a_node_while_both_peaks_fit(self):
        pods = [
            _simple_pod("p1", cpu=2, memory=3, eligible_nodes=("n1",)),
            _simple_pod("p2", cpu=3, memory=1, eligible_nodes=("n1",)),
        ]
        nodes = [Node("n1", cpu_available=5, memory_available=4)]

        result = place_group(pods, nodes)

        self.assertTrue(result.success)
        residual = result.residuals["n1"]
        self.assertEqual(residual.cpu_available, 0)
        self.assertEqual(residual.memory_available, 0)

    def test_exact_capacity_fit_is_accepted(self):
        pods = [
            _simple_pod("p1", cpu=2.5, memory=1.5,
                        eligible_nodes=("n1",), overhead=0.5),
            _simple_pod("p2", cpu=1.0, memory=2.0, eligible_nodes=("n1",)),
        ]
        # p1 peak = (3.0, 2.0); totals = (4.0, 4.0) exactly.
        nodes = [Node("n1", cpu_available=4.0, memory_available=4.0)]

        result = place_group(pods, nodes)

        self.assertTrue(result.success)
        self.assertEqual(result.placement["p1"], "n1")
        self.assertEqual(result.placement["p2"], "n1")
        self.assertEqual(result.residuals["n1"].cpu_available, 0.0)
        self.assertEqual(result.residuals["n1"].memory_available, 0.0)

    def test_empty_group_succeeds_with_original_capacity(self):
        nodes = [Node("n1", cpu_available=3, memory_available=4)]

        result = place_group([], nodes)

        self.assertTrue(result.success)
        self.assertEqual(result.placement, {})
        self.assertEqual(result.residuals["n1"].cpu_available, 3)
        self.assertEqual(result.residuals["n1"].memory_available, 4)


class InterleavedBacktrackingTest(unittest.TestCase):
    def test_greedy_choice_is_undone_for_more_constrained_pod(self):
        # p1 has two options; p2 can only use n1.  A greedy placement of p1
        # onto n1 would make p2 impossible, so backtracking must move p1.
        pods = [
            _simple_pod("p1", cpu=4, memory=4,
                        eligible_nodes=("n1", "n2")),
            _simple_pod("p2", cpu=4, memory=4, eligible_nodes=("n1",)),
        ]
        nodes = [
            Node("n1", cpu_available=5, memory_available=5),
            Node("n2", cpu_available=5, memory_available=5),
        ]

        result = place_group(pods, nodes)

        self.assertTrue(result.success)
        self.assertEqual(result.placement["p1"], "n2")
        self.assertEqual(result.placement["p2"], "n1")

    def test_three_way_interleaving_across_two_resources(self):
        # n1 is the only node with enough memory for the memory-heavy pod;
        # n2 is the only node with enough CPU for the cpu-heavy pod.
        pods = [
            _simple_pod("flex", cpu=2, memory=2,
                        eligible_nodes=("n1", "n2")),
            _simple_pod("mem-only", cpu=1, memory=6,
                        eligible_nodes=("n1",)),
            _simple_pod("cpu-only", cpu=6, memory=1,
                        eligible_nodes=("n2",)),
        ]
        nodes = [
            Node("n1", cpu_available=4, memory_available=8),
            Node("n2", cpu_available=8, memory_available=4),
        ]

        result = place_group(pods, nodes)

        self.assertTrue(result.success)
        # The constrained pods are forced; their only eligible node works.
        self.assertEqual(result.placement["mem-only"], "n1")
        self.assertEqual(result.placement["cpu-only"], "n2")
        # flex cannot join mem-only on n1 (memory 6 + 2 > 8? no, it fits:
        # 8 == 8), so verify every node stays within BOTH capacities and
        # the whole group is covered, regardless of flex's exact node.
        self.assertEqual(set(result.placement), {"flex", "mem-only",
                                                  "cpu-only"})
        usage = {node.name: [0.0, 0.0] for node in nodes}
        for pod in pods:
            peak = result.peaks[pod.name]
            node_name = result.placement[pod.name]
            usage[node_name][0] += peak.cpu_peak
            usage[node_name][1] += peak.memory_peak
        for node in nodes:
            self.assertLessEqual(usage[node.name][0], node.cpu_available)
            self.assertLessEqual(usage[node.name][1], node.memory_available)

    def test_peak_includes_resident_init_and_overhead_in_placement(self):
        pod = Pod(
            name="p",
            inits=(
                Container(
                    name="sidecar-init",
                    cpu=2,
                    memory=2,
                    kind="init",
                    resident=True,
                ),
            ),
            apps=(Container(name="app", cpu=3, memory=3, kind="app"),),
            cpu_overhead=1,
            memory_overhead=1,
            eligible_nodes=("n1",),
        )
        # Peak stage = app:all at (5, 5) raw; plus overhead -> (6, 6).
        too_small = place_group(
            [pod], [Node("n1", cpu_available=5.9, memory_available=10)]
        )
        fits = place_group(
            [pod], [Node("n1", cpu_available=6, memory_available=6)]
        )

        self.assertFalse(too_small.success)
        self.assertTrue(fits.success)
        self.assertEqual(fits.peaks["p"].cpu_peak, 6)
        self.assertEqual(fits.peaks["p"].memory_peak, 6)


class FailureAndImmutabilityTest(unittest.TestCase):
    def test_no_solution_preserves_node_residuals_exactly(self):
        pods = [
            _simple_pod("p1", cpu=6, memory=1,
                        eligible_nodes=("n1", "n2")),
            _simple_pod("p2", cpu=6, memory=1, eligible_nodes=("n1",)),
        ]
        nodes = [
            Node("n1", cpu_available=5, memory_available=5),
            Node("n2", cpu_available=5, memory_available=5),
        ]
        snapshot = [(n.name, n.cpu_available, n.memory_available)
                    for n in nodes]

        result = place_group(pods, nodes)

        self.assertFalse(result.success)
        self.assertEqual(result.placement, {})
        self.assertIsNotNone(result.reason)
        # Reported residuals equal the original remaining capacity.
        for name, cpu, memory in snapshot:
            self.assertEqual(
                result.residuals[name].cpu_available, cpu
            )
            self.assertEqual(
                result.residuals[name].memory_available, memory
            )
        # The Node objects themselves were never mutated.
        self.assertEqual(
            [(n.name, n.cpu_available, n.memory_available) for n in nodes],
            snapshot,
        )

    def test_memory_constraint_alone_can_make_it_infeasible(self):
        pods = [
            _simple_pod("p1", cpu=1, memory=4, eligible_nodes=("n1",)),
            _simple_pod("p2", cpu=1, memory=4, eligible_nodes=("n1",)),
        ]
        nodes = [Node("n1", cpu_available=10, memory_available=7)]

        result = place_group(pods, nodes)

        self.assertFalse(result.success)
        self.assertEqual(result.residuals["n1"].memory_available, 7)

    def test_pod_without_eligible_nodes_cannot_be_placed(self):
        pods = [_simple_pod("stranded", cpu=1, memory=1, eligible_nodes=())]
        nodes = [Node("n1", cpu_available=9, memory_available=9)]

        result = place_group(pods, nodes)

        self.assertFalse(result.success)
        self.assertEqual(result.residuals["n1"].cpu_available, 9)

    def test_peaks_are_still_returned_on_failure(self):
        pods = [_simple_pod("p", cpu=3, memory=2, eligible_nodes=("n1",))]
        nodes = [Node("n1", cpu_available=1, memory_available=1)]

        result = place_group(pods, nodes)

        self.assertFalse(result.success)
        self.assertIn("p", result.peaks)
        self.assertEqual(result.peaks["p"].cpu_peak, 3)
        self.assertEqual(result.peaks["p"].memory_peak, 2)


class LimitsAndValidationTest(unittest.TestCase):
    def test_max_scale_eight_pods_six_nodes(self):
        pods = [
            _simple_pod(f"p{i}", cpu=1, memory=1,
                        eligible_nodes=("n1", "n2", "n3", "n4",
                                        "n5", "n6"))
            for i in range(MAX_PODS)
        ]
        nodes = [
            Node(f"n{i}", cpu_available=2, memory_available=2)
            for i in range(1, MAX_NODES + 1)
        ]

        result = place_group(pods, nodes)

        self.assertTrue(result.success)
        self.assertEqual(len(result.placement), MAX_PODS)
        # Eight pods over six nodes: verify full coverage, per-node capacity,
        # and that the reported residuals match the actual reservations.
        usage = {node.name: [0, 0] for node in nodes}
        for pod in pods:
            node_name = result.placement[pod.name]
            usage[node_name][0] += result.peaks[pod.name].cpu_peak
            usage[node_name][1] += result.peaks[pod.name].memory_peak
        for node in nodes:
            used_cpu, used_memory = usage[node.name]
            self.assertLessEqual(used_cpu, node.cpu_available)
            self.assertLessEqual(used_memory, node.memory_available)
            self.assertEqual(
                result.residuals[node.name].cpu_available,
                node.cpu_available - used_cpu,
            )
            self.assertEqual(
                result.residuals[node.name].memory_available,
                node.memory_available - used_memory,
            )
        # Pigeonhole: with 8 pods on 6 nodes, at least two nodes host 2 pods.
        multi_pod_nodes = sum(1 for load in usage.values() if load[0] > 1)
        self.assertGreaterEqual(multi_pod_nodes, 2)

    def test_too_many_pods_rejected(self):
        pods = [
            _simple_pod(f"p{i}", cpu=1, memory=1, eligible_nodes=("n1",))
            for i in range(MAX_PODS + 1)
        ]
        with self.assertRaises(PlacementInputError):
            place_group(pods, [Node("n1", 100, 100)])

    def test_too_many_nodes_rejected(self):
        nodes = [Node(f"n{i}", 1, 1) for i in range(MAX_NODES + 1)]
        with self.assertRaises(PlacementInputError):
            place_group([], nodes)

    def test_unknown_eligible_node_rejected(self):
        pod = _simple_pod("p", 1, 1, eligible_nodes=("ghost",))
        with self.assertRaises(PlacementInputError):
            place_group([pod], [Node("n1", 5, 5)])

    def test_negative_and_nan_resources_rejected(self):
        with self.assertRaises(PlacementInputError):
            place_group(
                [_simple_pod("p", -1, 1, eligible_nodes=("n1",))],
                [Node("n1", 5, 5)],
            )
        with self.assertRaises(PlacementInputError):
            place_group([], [Node("n1", float("nan"), 5)])

    def test_duplicate_names_and_bad_container_kind_rejected(self):
        with self.assertRaises(PlacementInputError):
            place_group(
                [
                    _simple_pod("dup", 1, 1, ("n1",)),
                    _simple_pod("dup", 1, 1, ("n1",)),
                ],
                [Node("n1", 5, 5)],
            )
        bad = Pod(
            name="bad",
            apps=(Container("x", 1, 1, kind="sidecar"),),
            eligible_nodes=("n1",),
        )
        with self.assertRaises(PlacementInputError):
            place_group([bad], [Node("n1", 5, 5)])

    def test_resident_app_container_rejected(self):
        bad = Pod(
            name="bad",
            apps=(Container("x", 1, 1, kind="app", resident=True),),
            eligible_nodes=("n1",),
        )
        with self.assertRaises(PlacementInputError):
            place_group([bad], [Node("n1", 5, 5)])


class DeterminismTest(unittest.TestCase):
    def test_repeated_calls_give_the_same_assignment(self):
        pods = [
            _simple_pod(f"p{i}", cpu=1, memory=1,
                        eligible_nodes=("n1", "n2"))
            for i in range(5)
        ]
        nodes = [
            Node("n1", cpu_available=3, memory_available=3),
            Node("n2", cpu_available=3, memory_available=3),
        ]

        first = place_group(pods, nodes).placement
        for _ in range(5):
            self.assertEqual(place_group(pods, nodes).placement, first)


if __name__ == "__main__":
    unittest.main()
