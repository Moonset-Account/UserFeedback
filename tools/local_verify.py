#!/usr/bin/env python3
"""Local verification wrapper for SEC-20261114-004.

Runs, in order:

1. The unittest suite obtained via
   ``unittest.defaultTestLoader.discover("tests")``.  The run fails (exit
   != 0) if zero tests are discovered, if every discovered test is
   skipped, or if any test fails/errors.  unittest's "zero tests exit 0"
   behaviour is explicitly NOT treated as a pass.
2. The delivered sample/boundary assertions (SEC004-Sxx).  Each scenario
   in ``samples/`` is executed through the real CLI entry point
   (``app.py``) and every assertion prints its expected and actual value.

Usage:
    /usr/local/bin/python3 tools/local_verify.py
"""

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = "/usr/local/bin/python3"


def run_unit_tests():
    print("=" * 70)
    print("[unit] discovering tests via unittest.defaultTestLoader.discover")
    suite = unittest.defaultTestLoader.discover("tests", top_level_dir=".")
    discovered = suite.countTestCases()
    print("[unit] discovered %d test case(s)" % discovered)
    if discovered < 1:
        print("[unit] FAIL: zero tests discovered")
        return False

    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)
    skipped = len(result.skipped)
    executed = result.testsRun - skipped
    print(
        "[unit] testsRun=%d skipped=%d executed=%d failures=%d errors=%d"
        % (result.testsRun, skipped, executed, len(result.failures), len(result.errors))
    )
    if executed < 1:
        print("[unit] FAIL: no test actually executed (all skipped)")
        return False
    if not result.wasSuccessful():
        print("[unit] FAIL: failures or errors present")
        return False
    print("[unit] PASS: %d test(s) executed successfully" % executed)
    return True


def run_scenario(sample_path):
    proc = subprocess.run(
        [PYTHON, os.path.join(ROOT, "app.py"), sample_path],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "app.py exited %d for %s: %s" % (proc.returncode, sample_path, proc.stderr)
        )
    return json.loads(proc.stdout)


def active_set(output):
    return sorted(
        (s["app"], s["issuer"], s["session_id"]) for s in output["active_sessions"]
    )


def call_sequence(output):
    return [[c["app"], c["outcome"]] for c in output["adapter_calls"]]


# Each entry: (case_id, sample file, [(assertion, expected, actual-fn), ...])
SAMPLE_CHECKS = [
    (
        "SEC004-S01",
        "samples/session_logout.json",
        "single-session event purges only (issuer, session_id) matches; "
        "duplicate delivery recorded once",
        [
            ("purge status", "completed", lambda o: o["purges"][0]["status"]),
            ("apps targeted", ["A", "B"],
             lambda o: sorted(o["purges"][0]["apps"].keys())),
            ("terminated session ids per app",
             {"A": ["s1"], "B": ["s1"]},
             lambda o: {a: d["session_ids"]
                        for a, d in o["purges"][0]["apps"].items()}),
            ("second delivery flagged duplicate", True,
             lambda o: o["events"][1]["duplicate"]),
            ("duplicate counted on the single purge record", 1,
             lambda o: o["purges"][0]["duplicate_count"]),
            ("adapter calls (duplicate caused none)", 2,
             lambda o: len(o["adapter_calls"])),
            ("sessions still active",
             [("A", "idp-1", "s2"), ("A", "idp-2", "s1"), ("B", "idp-1", "s9")],
             active_set),
        ],
    ),
    (
        "SEC004-S02",
        "samples/account_purge_retry.json",
        "account purge with transient app-B failure: pending state kept, "
        "retry redoes only app B, re-login after issued_at survives",
        [
            ("final purge status", "completed",
             lambda o: o["purges"][0]["status"]),
            ("app B attempts (1 initial + 1 retry)", 2,
             lambda o: o["purges"][0]["apps"]["B"]["attempts"]),
            ("app B attempt history", ["failed", "success"],
             lambda o: [h["result"]
                        for h in o["purges"][0]["apps"]["B"]["history"]]),
            ("app A attempts (not retried)", 1,
             lambda o: o["purges"][0]["apps"]["A"]["attempts"]),
            ("retried event ids", ["e1"], lambda o: o["retried"]),
            ("adapter call sequence",
             [["A", "success"], ["B", "timeout"], ["B", "success"]],
             call_sequence),
            ("purge targeted only pre-issued_at sessions",
             ["s1", "s2"],
             lambda o: sorted(o["purges"][0]["apps"]["B"]["session_ids"])),
            ("sessions still active (re-login s3 survives)",
             [("B", "idp-1", "s3")], active_set),
        ],
    ),
    (
        "SEC004-S03",
        "samples/boundary_issued_at.json",
        "boundary: login_time == issued_at is purged, login_time > issued_at "
        "is not",
        [
            ("terminated session ids", ["edge", "old"],
             lambda o: sorted(o["purges"][0]["apps"]["A"]["session_ids"])),
            ("sessions still active", [("A", "idp-1", "new")], active_set),
        ],
    ),
]


def run_sample_checks():
    print("=" * 70)
    all_ok = True
    for case_id, sample, description, assertions in SAMPLE_CHECKS:
        print("[%s] %s" % (case_id, description))
        print("[%s] scenario: %s" % (case_id, sample))
        try:
            output = run_scenario(os.path.join(ROOT, sample))
        except Exception as exc:  # noqa: BLE001 - report and fail the case
            print("[%s] FAIL: scenario could not run: %s" % (case_id, exc))
            all_ok = False
            continue
        for name, expected, actual_fn in assertions:
            try:
                actual = actual_fn(output)
            except Exception as exc:  # noqa: BLE001 - keep per-item evidence
                print("[%s]   FAIL %s: could not extract actual value: %s"
                      % (case_id, name, exc))
                all_ok = False
                continue
            ok = actual == expected
            all_ok = all_ok and ok
            print(
                "[%s]   %s %s: expected=%r actual=%r"
                % (case_id, "PASS" if ok else "FAIL", name, expected, actual)
            )
    return all_ok


def main():
    os.chdir(ROOT)
    unit_ok = run_unit_tests()
    samples_ok = run_sample_checks()
    print("=" * 70)
    print("[summary] unit_tests=%s sample_assertions=%s"
          % ("PASS" if unit_ok else "FAIL", "PASS" if samples_ok else "FAIL"))
    if unit_ok and samples_ok:
        print("[summary] OVERALL: PASS")
        return 0
    print("[summary] OVERALL: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
