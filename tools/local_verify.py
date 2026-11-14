#!/usr/bin/env python3
"""Local verification wrapper for SEC-20261114-018.

Runs two evidence tracks and exits non-zero if either fails:

1. unittest suite discovered via ``unittest.defaultTestLoader.discover("tests")``.
   Fails when zero tests are discovered, when every test is skipped (zero
   actually executed), or when any failure/error occurs.  A zero-test exit
   code 0 from unittest is never treated as a pass.
2. Delivered sample/boundary scenarios under ``samples/``: each scenario is
   replayed through the real CLI entry (``app.py`` via subprocess) and every
   declared expectation is checked individually, printing the actual value
   next to the expected one.

Usage: /usr/local/bin/python3 tools/local_verify.py
"""

import glob
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_unittest_suite():
    os.chdir(ROOT)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    discovered = suite.countTestCases()
    print("[unittest] discovered %d test case(s)" % discovered)
    if discovered < 1:
        print("[unittest] FAIL: zero test cases discovered")
        return False
    runner = unittest.TextTestRunner(verbosity=1, stream=sys.stdout)
    result = runner.run(suite)
    skipped = len(result.skipped)
    actually_run = result.testsRun - skipped
    print(
        "[unittest] testsRun=%d skipped=%d actually_run=%d failures=%d errors=%d"
        % (result.testsRun, skipped, actually_run, len(result.failures), len(result.errors))
    )
    if actually_run < 1:
        print("[unittest] FAIL: zero tests actually executed (all skipped)")
        return False
    if not result.wasSuccessful():
        print("[unittest] FAIL: failures or errors present")
        return False
    print("[unittest] PASS")
    return True


def _resolve_path(document, path):
    node = document
    for part in path.split("."):
        if isinstance(node, list):
            node = node[int(part)]
        elif isinstance(node, dict):
            node = node[part]
        else:
            raise KeyError(path)
    return node


def run_sample_checks():
    sample_files = sorted(glob.glob(os.path.join(ROOT, "samples", "*.json")))
    if not sample_files:
        print("[samples] FAIL: no sample files found under samples/")
        return False
    all_ok = True
    for sample_path in sample_files:
        with open(sample_path, "r", encoding="utf-8") as handle:
            sample = json.load(handle)
        case_id = sample["case_id"]
        print(
            "[samples] %s (%s): %s"
            % (case_id, sample.get("requirement", "-"), sample.get("description", ""))
        )
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "app.py"), "-"],
            input=json.dumps({"commands": sample["commands"]}),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            print(
                "  [%s] FAIL: app.py exited %d: %s"
                % (case_id, proc.returncode, proc.stderr.strip())
            )
            all_ok = False
            continue
        document = json.loads(proc.stdout)
        for expectation in sample["expect"]:
            try:
                actual = _resolve_path(document, expectation["path"])
            except (KeyError, IndexError, ValueError):
                actual = "<missing>"
            expected = expectation["expected"]
            ok = actual == expected
            all_ok = all_ok and ok
            print(
                "  [%s] %s: expected=%r actual=%r -> %s"
                % (
                    case_id,
                    expectation["label"],
                    expected,
                    actual,
                    "PASS" if ok else "FAIL",
                )
            )
    return all_ok


def main():
    ok_unit = run_unittest_suite()
    ok_samples = run_sample_checks()
    if ok_unit and ok_samples:
        print("LOCAL VERIFY: PASS")
        return 0
    print("LOCAL VERIFY: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
