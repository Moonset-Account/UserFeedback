#!/usr/bin/env python3
"""Local verification wrapper for SEC-20261114-033.

Runs, in order:

1. The unittest suite discovered via ``unittest.defaultTestLoader.discover``
   on ``tests/``.  Fails (non-zero exit) when zero tests are discovered,
   when zero tests actually execute (e.g. all skipped), or on any
   failure/error.
2. The delivered demo scenario (``app.py --json``) and asserts, item by
   item, the expected per-request view-source sequence and outcomes.

Usage:  /usr/local/bin/python3 tools/local_verify.py
"""

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Expected (principal, view, source, ok) sequence emitted by app.py --json.
EXPECTED_DEMO_RECORDS = [
    ("bob", "detail", "loader", True),
    ("bob", "detail", "detail_cache", True),
    ("alice", "detail", "denied_perm_version_changed", False),
    ("alice", "summary", "summary_cache", True),
    ("alice", "detail", "loader", True),
    ("bob", "summary", "summary_fallback_refresh_failed", True),
    ("bob", "detail", "detail_fallback_refresh_failed", True),
    ("alice", "detail", "denied_refresh_failed", False),
]

EXPECTED_DEMO_OUTCOMES = {
    "alice_late": "denied",
    "alice_after_second_revoke": "denied",
}


def run_unit_suite() -> bool:
    sys.path.insert(0, ROOT)
    suite = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests"))
    count = suite.countTestCases()
    print(f"[verify] discovered {count} unit test case(s)")
    if count < 1:
        print("[verify] FAIL: zero tests discovered")
        return False
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    executed = result.testsRun - len(result.skipped)
    print(f"[verify] unit: ran={result.testsRun} skipped={len(result.skipped)} "
          f"executed={executed} failures={len(result.failures)} "
          f"errors={len(result.errors)}")
    if executed < 1:
        print("[verify] FAIL: zero tests actually executed (all skipped?)")
        return False
    if not result.wasSuccessful():
        print("[verify] FAIL: unit tests had failures/errors")
        return False
    print("[verify] unit suite: PASS")
    return True


def run_demo_assertions() -> bool:
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "app.py"), "--json"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        print(f"[verify] FAIL: app.py exited {proc.returncode}\n{proc.stderr}")
        return False

    records, outcomes = [], {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("type") == "record":
            records.append(msg)
        elif msg.get("type") == "outcomes":
            outcomes = msg.get("data", {})

    ok = True
    if len(records) != len(EXPECTED_DEMO_RECORDS):
        print(f"[verify] FAIL: expected {len(EXPECTED_DEMO_RECORDS)} demo "
              f"records, got {len(records)}")
        ok = False
    for i, expected in enumerate(EXPECTED_DEMO_RECORDS):
        if i < len(records):
            r = records[i]
            actual = (r["principal"], r["view"], r["source"], r["ok"])
            status = "PASS" if actual == expected else "FAIL"
            if actual != expected:
                ok = False
            print(f"[verify] demo[{i}] {status}: expected={expected} actual={actual}")
        else:
            print(f"[verify] demo[{i}] FAIL: missing record, expected={expected}")
            ok = False
    for key, want in EXPECTED_DEMO_OUTCOMES.items():
        got = outcomes.get(key)
        status = "PASS" if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"[verify] outcome {key}: {status} expected={want!r} actual={got!r}")
    return ok


def main() -> int:
    ok_unit = run_unit_suite()
    ok_demo = run_demo_assertions()
    if ok_unit and ok_demo:
        print("[verify] OVERALL PASS")
        return 0
    print("[verify] OVERALL FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
