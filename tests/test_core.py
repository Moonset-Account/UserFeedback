import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import (  # noqa: E402
    analyze,
    evaluate_mirror,
    mirror_covers,
    parse_port,
    simulate,
    trace_route,
)


def snapshot(links, collectors, mirrors, forwarding):
    return {
        "links": links,
        "collectors": collectors,
        "mirrors": mirrors,
        "forwarding": forwarding,
    }


def one_link_snapshot(mirrors, forwarding, collectors=None):
    return snapshot(
        links=[{"id": "L1", "a": "sw1:e1", "b": "sw2:e1"}],
        collectors=collectors or [{"id": "C1", "device": "sw9", "healthy": True}],
        mirrors=mirrors,
        forwarding=forwarding,
    )


def direction_entry(report, link, direction):
    for entry in report:
        if entry["link"] == link and entry["direction"] == direction:
            return entry
    raise AssertionError("missing entry %s %s" % (link, direction))


class ParsePortTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_port("sw1:eth1"), ("sw1", "eth1"))

    def test_invalid(self):
        for bad in ("sw1", ":eth1", "sw1:", ""):
            with self.assertRaises(ValueError):
                parse_port(bad)


class MirrorCoversTests(unittest.TestCase):
    def test_sides(self):
        self.assertTrue(mirror_covers({"direction": "egress"}, "egress"))
        self.assertFalse(mirror_covers({"direction": "egress"}, "ingress"))
        self.assertTrue(mirror_covers({"direction": "both"}, "ingress"))
        self.assertTrue(mirror_covers({"direction": "both"}, "egress"))

    def test_invalid_direction_rejected(self):
        with self.assertRaises(ValueError):
            mirror_covers({"id": "M?", "direction": "sideways"}, "egress")


class TraceRouteTests(unittest.TestCase):
    def test_direct(self):
        route, err = trace_route("sw1", "sw1", {})
        self.assertIsNone(err)
        self.assertEqual(route, ["sw1"])

    def test_two_forwarding_nodes_ok(self):
        fwd = {"sw1": "f1", "f1": "f2", "f2": "sw9"}
        route, err = trace_route("sw1", "sw9", fwd)
        self.assertIsNone(err)
        self.assertEqual(route, ["sw1", "f1", "f2", "sw9"])

    def test_three_forwarding_nodes_rejected(self):
        fwd = {"sw1": "f1", "f1": "f2", "f2": "f3", "f3": "sw9"}
        route, err = trace_route("sw1", "sw9", fwd)
        self.assertIsNone(route)
        self.assertEqual(err["stage"], "forwarding")
        self.assertIn("forwarding nodes", err["detail"])

    def test_loop_terminates(self):
        fwd = {"a": "b", "b": "a"}
        route, err = trace_route("a", "sw9", fwd)
        self.assertIsNone(route)
        self.assertEqual(err["stage"], "forwarding")
        self.assertIn("loop", err["detail"])

    def test_missing_hop(self):
        route, err = trace_route("a", "sw9", {})
        self.assertIsNone(route)
        self.assertEqual(err["stage"], "forwarding")
        self.assertIn("no forwarding entry", err["detail"])


class AnalyzeTests(unittest.TestCase):
    def test_both_directions_listed_per_link(self):
        report = analyze(one_link_snapshot([], {}))
        self.assertEqual(
            [(e["link"], e["direction"]) for e in report],
            [("L1", "a->b"), ("L1", "b->a")],
        )

    def test_no_mirror_breaks_at_source(self):
        report = analyze(one_link_snapshot([], {}))
        for entry in report:
            self.assertFalse(entry["observable"])
            self.assertEqual(entry["reason"]["stage"], "source")
            self.assertEqual(entry["attempts"], [])

    def test_egress_mirror_observes_a_to_b(self):
        mirrors = [{"id": "M1", "source": "sw1:e1", "direction": "egress",
                    "collector": "C1"}]
        fwd = {"sw1": "sw9"}
        report = analyze(one_link_snapshot(mirrors, fwd))
        ab = direction_entry(report, "L1", "a->b")
        self.assertTrue(ab["observable"])
        self.assertEqual(ab["paths"][0]["route"], ["sw1", "sw9"])
        self.assertIsNone(ab["reason"])
        ba = direction_entry(report, "L1", "b->a")
        self.assertFalse(ba["observable"])
        self.assertEqual(ba["reason"]["stage"], "source")

    def test_ingress_mirror_on_destination_observes_a_to_b(self):
        mirrors = [{"id": "M1", "source": "sw2:e1", "direction": "ingress",
                    "collector": "C1"}]
        fwd = {"sw2": "sw9"}
        report = analyze(one_link_snapshot(mirrors, fwd))
        self.assertTrue(direction_entry(report, "L1", "a->b")["observable"])
        self.assertFalse(direction_entry(report, "L1", "b->a")["observable"])

    def test_both_direction_mirror_covers_both_link_directions(self):
        mirrors = [{"id": "M1", "source": "sw1:e1", "direction": "both",
                    "collector": "C1"}]
        fwd = {"sw1": "sw9"}
        report = analyze(one_link_snapshot(mirrors, fwd))
        self.assertTrue(direction_entry(report, "L1", "a->b")["observable"])
        self.assertTrue(direction_entry(report, "L1", "b->a")["observable"])

    def test_unhealthy_collector_breaks_at_collector(self):
        collectors = [{"id": "C1", "device": "sw9", "healthy": False,
                       "status": "offline"}]
        mirrors = [{"id": "M1", "source": "sw1:e1", "direction": "egress",
                    "collector": "C1"}]
        report = analyze(one_link_snapshot(mirrors, {"sw1": "sw9"}, collectors))
        ab = direction_entry(report, "L1", "a->b")
        self.assertFalse(ab["observable"])
        self.assertEqual(ab["reason"]["stage"], "collector")
        self.assertIn("offline", ab["reason"]["detail"])

    def test_unknown_collector_breaks_at_collector(self):
        mirrors = [{"id": "M1", "source": "sw1:e1", "direction": "egress",
                    "collector": "C?"}]
        report = analyze(one_link_snapshot(mirrors, {}))
        ab = direction_entry(report, "L1", "a->b")
        self.assertEqual(ab["reason"]["stage"], "collector")
        self.assertIn("unknown collector", ab["reason"]["detail"])

    def test_forwarding_loop_breaks_at_forwarding_and_terminates(self):
        mirrors = [{"id": "M1", "source": "sw1:e1", "direction": "egress",
                    "collector": "C1"}]
        fwd = {"sw1": "sw2", "sw2": "sw1"}
        report = analyze(one_link_snapshot(mirrors, fwd))
        ab = direction_entry(report, "L1", "a->b")
        self.assertFalse(ab["observable"])
        self.assertEqual(ab["reason"]["stage"], "forwarding")
        self.assertIn("loop", ab["reason"]["detail"])

    def test_too_many_forwarding_nodes_breaks_at_forwarding(self):
        mirrors = [{"id": "M1", "source": "sw1:e1", "direction": "egress",
                    "collector": "C1"}]
        fwd = {"sw1": "f1", "f1": "f2", "f2": "f3", "f3": "sw9"}
        report = analyze(one_link_snapshot(mirrors, fwd))
        ab = direction_entry(report, "L1", "a->b")
        self.assertEqual(ab["reason"]["stage"], "forwarding")
        self.assertIn("forwarding nodes", ab["reason"]["detail"])

    def test_attempts_list_all_failures(self):
        collectors = [
            {"id": "C1", "device": "sw9", "healthy": True},
            {"id": "C2", "device": "sw8", "healthy": False},
        ]
        mirrors = [
            {"id": "M1", "source": "sw1:e1", "direction": "egress",
             "collector": "C1"},
            {"id": "M2", "source": "sw1:e1", "direction": "egress",
             "collector": "C2"},
        ]
        fwd = {"sw1": "f1"}  # dead end for both collectors
        report = analyze(one_link_snapshot(mirrors, fwd, collectors))
        ab = direction_entry(report, "L1", "a->b")
        self.assertFalse(ab["observable"])
        self.assertEqual(len(ab["attempts"]), 2)
        self.assertTrue(all(a["stage"] == "forwarding" for a in ab["attempts"]))


class EvaluateMirrorTests(unittest.TestCase):
    def test_decommissioned_collector_fails_at_collector(self):
        collectors = {"C1": {"id": "C1", "device": "sw9", "healthy": True}}
        mirror = {"id": "M1", "source": "sw1:e1", "collector": "C1"}
        path, failure = evaluate_mirror(mirror, collectors, {"sw1": "sw9"},
                                        disabled={"C1"})
        self.assertIsNone(path)
        self.assertEqual(failure["stage"], "collector")
        self.assertIn("decommissioned", failure["detail"])


class SimulateTests(unittest.TestCase):
    def setUp(self):
        self.snap = snapshot(
            links=[{"id": "L1", "a": "sw1:e1", "b": "sw2:e1"}],
            collectors=[
                {"id": "C1", "device": "sw9", "healthy": True},
                {"id": "C2", "device": "sw9", "healthy": True},
            ],
            mirrors=[
                {"id": "M1", "source": "sw1:e1", "direction": "egress",
                 "collector": "C1"},
                {"id": "M2", "source": "sw1:e1", "direction": "egress",
                 "collector": "C2"},
                {"id": "M3", "source": "sw2:e1", "direction": "egress",
                 "collector": "C1"},
            ],
            forwarding={"sw1": "sw9", "sw2": "sw9"},
        )

    def test_lost_and_retained(self):
        # Baseline: a->b via M1(C1)+M2(C2); b->a via M3(C1) only.
        result = simulate(self.snap, ["C1"])
        lost = {(e["link"], e["direction"]) for e in result["lost"]}
        retained = {(e["link"], e["direction"]) for e in result["retained"]}
        self.assertEqual(lost, {("L1", "b->a")})
        self.assertEqual(retained, {("L1", "a->b")})
        remaining = result["retained"][0]["remaining_paths"]
        self.assertEqual([p["mirror"] for p in remaining], ["M2"])
        lost_prev = result["lost"][0]["previous_paths"]
        self.assertEqual([p["mirror"] for p in lost_prev], ["M3"])

    def test_decommissioning_nothing_changes_nothing(self):
        result = simulate(self.snap, [])
        self.assertEqual(result["lost"], [])
        self.assertEqual(len(result["retained"]), 2)

    def test_unknown_decommission_flagged(self):
        result = simulate(self.snap, ["C?"])
        self.assertEqual(result["unknown_collectors"], ["C?"])
        self.assertEqual(result["lost"], [])


if __name__ == "__main__":
    unittest.main()
