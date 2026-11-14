#!/usr/bin/env python3
"""Local verification entry point.

1. Discovers the unittest suite via unittest.defaultTestLoader.discover("tests")
   and refuses to pass when zero tests are discovered, every test is skipped,
   or any test fails/errors.
2. Runs the delivered sample (examples/) through app.py and checks the
   expected attribution outcomes one by one, printing actual vs expected
   for every check.

Exit code 0 only when both stages succeed. Finishes in well under a minute.
"""

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "examples")


def run_unit_tests():
    os.chdir(ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    count = suite.countTestCases()
    print("[verify] discovered %d test case(s)" % count)
    if count < 1:
        print("[verify] FAIL: zero tests discovered")
        return False
    result = unittest.TextTestRunner(verbosity=1, stream=sys.stdout).run(suite)
    executed = result.testsRun - len(result.skipped)
    print("[verify] testsRun=%d skipped=%d executed=%d failures=%d errors=%d"
          % (result.testsRun, len(result.skipped), executed,
             len(result.failures), len(result.errors)))
    if executed < 1:
        print("[verify] FAIL: zero tests actually executed (all skipped?)")
        return False
    if not result.wasSuccessful():
        print("[verify] FAIL: unit tests had failures or errors")
        return False
    return True


def _run_app(session_files, process_files):
    cmd = [sys.executable, "-I", os.path.join(ROOT, "app.py")]
    for f in session_files:
        cmd += ["--sessions", os.path.join(EXAMPLES, f)]
    for f in process_files:
        cmd += ["--processes", os.path.join(EXAMPLES, f)]
    cmd += ["--calibration", os.path.join(EXAMPLES, "calibration.csv")]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def run_sample_checks():
    sessions = ["sessions_a.csv", "sessions_b.csv"]
    processes = ["processes_a.jsonl", "processes_b.jsonl"]
    res = _run_app(sessions, processes)
    if res.returncode != 0:
        print("[verify] FAIL: app.py exited %d on the sample: %s"
              % (res.returncode, res.stderr.strip()))
        return False
    try:
        report = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        print("[verify] FAIL: sample output is not valid JSON: %s" % exc)
        return False

    results = []

    def check(case_id, desc, actual, expected):
        ok = actual == expected
        results.append(ok)
        print("[sample %s] %s: %s | expected=%r actual=%r"
              % (case_id, "PASS" if ok else "FAIL", desc, expected, actual))

    timeline = {e["event_id"]: e for e in report.get("timeline", [])}

    def attr(event_id):
        return timeline.get(event_id, {}).get("attribution", {})

    check("S1", "p-1 confirmed to alice",
          (attr("p-1").get("status"), attr("p-1").get("user")),
          ("confirmed", "alice"))
    check("S2", "p-1 merged duplicate exports keep 2 provenance entries",
          len(timeline.get("p-1", {}).get("provenance", [])), 2)
    check("S3", "p-1 utc interval from calibration (local +3600s +/-30s)",
          timeline.get("p-1", {}).get("utc_interval"),
          ["2026-11-14T09:29:30", "2026-11-14T09:30:30"])
    check("S4", "p-2 session not closed",
          attr("p-2").get("reason"), "session_not_closed")
    check("S5", "p-3 interval crosses session boundary",
          attr("p-3").get("reason"), "interval_outside_session")
    check("S6", "p-6 host without calibration",
          attr("p-6").get("reason"), "missing_calibration")
    check("S7", "p-7 session id only present in conflicted events",
          attr("p-7").get("reason"), "no_matching_session")
    check("S8", "p-8 confirmed to carol on db1 (UTC-8 +/-60s)",
          (attr("p-8").get("status"), attr("p-8").get("user")),
          ("confirmed", "carol"))
    check("S9", "timeline holds exactly the 6 non-conflicted processes",
          len(report.get("timeline", [])), 6)
    conflict_keys = sorted((c["kind"], c["source"], c["event_id"])
                           for c in report.get("conflicts", []))
    check("S10", "conflicts list session evt-6 and process p-9",
          conflict_keys,
          [("process", "edr", "p-9"), ("session", "siem", "evt-6")])

    swapped = _run_app(list(reversed(sessions)), list(reversed(processes)))
    check("S11", "swapping input file order yields identical output",
          swapped.returncode == 0 and swapped.stdout == res.stdout, True)

    return all(results)


def main():
    ok_tests = run_unit_tests()
    ok_samples = run_sample_checks()
    if ok_tests and ok_samples:
        print("[verify] OK: unit tests and sample checks passed")
        return 0
    print("[verify] FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
