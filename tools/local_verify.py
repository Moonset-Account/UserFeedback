#!/usr/bin/env python3
"""Local verification wrapper for the mirror-collector decommission precheck.

Runs (from the workspace root):
  1. the unittest suite discovered in tests/ — fails on zero discovered
     tests, zero actually-executed (non-skipped) tests, or any failure/error;
  2. the delivered sample/edge assertions (CASE-01 .. CASE-06), each mapped
     to an explicit requirement of the original prompt, printing the actual
     vs expected result per case.

Exit code 0 only if every step passes.
"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")
BASIC = os.path.join(ROOT, "samples", "basic.json")
EDGE_HOPS = os.path.join(ROOT, "samples", "edge_hops.json")


def run_unittests():
    os.chdir(ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    count = suite.countTestCases()
    print("[verify] discovered %d unittest case(s)" % count)
    if count < 1:
        print("[verify] FAIL: zero tests discovered in tests/")
        return False
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    executed = result.testsRun - len(result.skipped)
    print("[verify] unittest: ran=%d skipped=%d executed=%d failures=%d errors=%d"
          % (result.testsRun, len(result.skipped), executed,
             len(result.failures), len(result.errors)))
    if executed < 1:
        print("[verify] FAIL: no test actually executed (all skipped?)")
        return False
    if not result.wasSuccessful():
        print("[verify] FAIL: unittest suite had failures/errors")
        return False
    print("[verify] unittest suite: PASS")
    return True


def run_app(*args):
    proc = subprocess.run(
        [sys.executable, APP, *args],
        capture_output=True, text=True, cwd=ROOT, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError("app.py exited %d: %s" % (proc.returncode, proc.stderr))
    return json.loads(proc.stdout)


def find_entry(report, link, direction):
    for e in report:
        if e["link"] == link and e["direction"] == direction:
            return e
    raise AssertionError("missing entry %s %s" % (link, direction))


def run_sample_cases():
    baseline = run_app(BASIC)["directions"]
    simulated = run_app(BASIC, "--decommission", "C2")["simulation"]
    edge = run_app(EDGE_HOPS)["directions"]

    cases = []

    def case(case_id, requirement, actual, expected, ok):
        cases.append({
            "case_id": case_id,
            "requirement": requirement,
            "actual": actual,
            "expected": expected,
            "ok": bool(ok),
        })

    e = find_entry(baseline, "L1", "a->b")
    routes = {p["mirror"]: p["route"] for p in e["paths"]}
    case("CASE-01",
         "observable direction carries at least one valid mirror path",
         {"observable": e["observable"], "routes": routes},
         {"observable": True,
          "routes": {"M1": ["sw1", "sw4", "sw5", "sw9"],
                     "M8": ["sw2", "sw6", "sw8"]}},
         e["observable"] and routes == {
             "M1": ["sw1", "sw4", "sw5", "sw9"],
             "M8": ["sw2", "sw6", "sw8"]})

    e = find_entry(baseline, "L3", "a->b")
    case("CASE-02",
         "unobservable direction broken at collector is classified 'collector'",
         {"observable": e["observable"], "stage": e["reason"]["stage"]},
         {"observable": False, "stage": "collector"},
         not e["observable"] and e["reason"]["stage"] == "collector")

    e = find_entry(baseline, "L3", "b->a")
    case("CASE-03",
         "direction without any covering mirror is classified 'source'",
         {"observable": e["observable"], "stage": e["reason"]["stage"]},
         {"observable": False, "stage": "source"},
         not e["observable"] and e["reason"]["stage"] == "source")

    e = find_entry(baseline, "L4", "a->b")
    detail = e["reason"]["detail"]
    case("CASE-04",
         "forwarding loop terminates and is classified 'forwarding'",
         {"observable": e["observable"], "stage": e["reason"]["stage"],
          "detail": detail},
         {"observable": False, "stage": "forwarding", "detail_contains": "loop"},
         not e["observable"] and e["reason"]["stage"] == "forwarding"
         and "loop" in detail)

    lost = {(x["link"], x["direction"]) for x in simulated["lost"]}
    retained = {(x["link"], x["direction"]) for x in simulated["retained"]}
    case("CASE-05",
         "decommissioning C2 lists directions losing their last path and "
         "keeps directions still covered by other collectors",
         {"lost": sorted(lost), "retained": sorted(retained)},
         {"lost": [["L1", "b->a"], ["L2", "a->b"], ["L2", "b->a"]],
          "retained": [["L1", "a->b"]]},
         lost == {("L1", "b->a"), ("L2", "a->b"), ("L2", "b->a")}
         and retained == {("L1", "a->b")})

    e = find_entry(edge, "LX", "a->b")
    detail = e["reason"]["detail"]
    case("CASE-06",
         "mirror path needing more than two forwarding nodes is rejected "
         "at the forwarding stage",
         {"observable": e["observable"], "stage": e["reason"]["stage"],
          "detail": detail},
         {"observable": False, "stage": "forwarding",
          "detail_contains": "forwarding nodes"},
         not e["observable"] and e["reason"]["stage"] == "forwarding"
         and "forwarding nodes" in detail)

    all_ok = True
    for c in cases:
        status = "PASS" if c["ok"] else "FAIL"
        all_ok = all_ok and c["ok"]
        print("[verify] %s %s: %s" % (c["case_id"], status, c["requirement"]))
        print("         actual   = %s" % json.dumps(c["actual"], ensure_ascii=False))
        print("         expected = %s" % json.dumps(c["expected"], ensure_ascii=False))
    return all_ok


def main():
    ok_tests = run_unittests()
    ok_cases = run_sample_cases()
    if ok_tests and ok_cases:
        print("[verify] OVERALL: PASS")
        return 0
    print("[verify] OVERALL: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
