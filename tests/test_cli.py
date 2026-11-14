import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")
BASIC = os.path.join(ROOT, "samples", "basic.json")
EDGE_HOPS = os.path.join(ROOT, "samples", "edge_hops.json")


def run_app(*args):
    return subprocess.run(
        [sys.executable, APP, *args],
        capture_output=True, text=True, cwd=ROOT, timeout=30,
    )


def entry(report, link, direction):
    for e in report["directions"]:
        if e["link"] == link and e["direction"] == direction:
            return e
    raise AssertionError("missing %s %s" % (link, direction))


class CliBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = run_app(BASIC)
        assert proc.returncode == 0, proc.stderr
        cls.out = json.loads(proc.stdout)

    def test_every_link_has_two_directions(self):
        got = [(e["link"], e["direction"]) for e in self.out["directions"]]
        expected = []
        for link in ("L1", "L2", "L3", "L4"):
            expected += [(link, "a->b"), (link, "b->a")]
        self.assertEqual(got, expected)

    def test_observable_direction_has_valid_path(self):
        e = entry(self.out, "L1", "a->b")
        self.assertTrue(e["observable"])
        routes = {p["mirror"]: p["route"] for p in e["paths"]}
        self.assertEqual(routes["M1"], ["sw1", "sw4", "sw5", "sw9"])
        self.assertEqual(routes["M8"], ["sw2", "sw6", "sw8"])

    def test_collector_break(self):
        e = entry(self.out, "L3", "a->b")
        self.assertFalse(e["observable"])
        self.assertEqual(e["reason"]["stage"], "collector")

    def test_source_break(self):
        e = entry(self.out, "L3", "b->a")
        self.assertFalse(e["observable"])
        self.assertEqual(e["reason"]["stage"], "source")

    def test_forwarding_loop_break(self):
        e = entry(self.out, "L4", "a->b")
        self.assertFalse(e["observable"])
        self.assertEqual(e["reason"]["stage"], "forwarding")
        self.assertIn("loop", e["reason"]["detail"])


class CliEdgeHopsTests(unittest.TestCase):
    def test_three_forwarding_nodes_rejected(self):
        proc = run_app(EDGE_HOPS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        e = entry(out, "LX", "a->b")
        self.assertFalse(e["observable"])
        self.assertEqual(e["reason"]["stage"], "forwarding")
        self.assertIn("forwarding nodes", e["reason"]["detail"])


class CliSimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = run_app(BASIC, "--decommission", "C2")
        assert proc.returncode == 0, proc.stderr
        cls.sim = json.loads(proc.stdout)["simulation"]

    def test_lost_directions(self):
        lost = {(e["link"], e["direction"]) for e in self.sim["lost"]}
        self.assertEqual(lost, {("L1", "b->a"), ("L2", "a->b"), ("L2", "b->a")})

    def test_retained_directions(self):
        retained = {(e["link"], e["direction"]) for e in self.sim["retained"]}
        self.assertEqual(retained, {("L1", "a->b")})
        remaining = self.sim["retained"][0]["remaining_paths"]
        self.assertEqual([p["collector"] for p in remaining], ["C1"])

    def test_report_reflects_decommission(self):
        e = entry({"directions": self.sim["report"]}, "L1", "b->a")
        self.assertFalse(e["observable"])
        self.assertEqual(e["reason"]["stage"], "collector")
        self.assertIn("decommissioned", e["reason"]["detail"])


class CliErrorHandlingTests(unittest.TestCase):
    def test_missing_file_exits_nonzero(self):
        proc = run_app(os.path.join(ROOT, "samples", "nope.json"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("error", proc.stderr)

    def test_invalid_snapshot_exits_nonzero(self):
        bad = os.path.join(ROOT, "tests", "_bad_snapshot.json")
        with open(bad, "w", encoding="utf-8") as fh:
            json.dump({"links": [{"id": "L", "a": "sw1", "b": "sw2:e1"}],
                       "collectors": [], "mirrors": [], "forwarding": {}}, fh)
        try:
            proc = run_app(bad)
            self.assertNotEqual(proc.returncode, 0)
        finally:
            os.unlink(bad)


if __name__ == "__main__":
    unittest.main()
