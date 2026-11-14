#!/usr/bin/env python3
"""Local verification wrapper for SEC-20261114-026.

Runs (from the workspace root):
  1. the unittest suite discovered via
     unittest.defaultTestLoader.discover("tests") -- requires at least
     one discovered test and at least one actually executed, non-skipped
     test; zero tests, all-skipped, errors or failures exit non-zero;
  2. the delivered sample/boundary assertions (case ids SAMPLE-* and
     BOUND-*), each printed with expected vs actual results.

Usage:  /usr/local/bin/python3 tools/local_verify.py
Exit code 0 only if every step passes.
"""

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def run_unittest_suite():
    print("=== step 1: unittest discovery + run ===")
    suite = unittest.defaultTestLoader.discover("tests")
    count = suite.countTestCases()
    print("discovered test cases: %d" % count)
    if count < 1:
        print("FAIL: zero tests discovered")
        return False
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    executed = result.testsRun
    skipped = len(result.skipped)
    executed_not_skipped = executed - skipped
    print(
        "executed: %d, skipped: %d, executed-not-skipped: %d"
        % (executed, skipped, executed_not_skipped)
    )
    if executed_not_skipped < 1:
        print("FAIL: no test actually executed without being skipped")
        return False
    if not result.wasSuccessful():
        print("FAIL: unittest suite reported failures/errors")
        return False
    print("PASS: unittest suite")
    return True


def check(case_id, description, expected, actual):
    ok = expected == actual
    status = "PASS" if ok else "FAIL"
    print("[%s] %s" % (status, case_id))
    print("    what:     %s" % description)
    print("    expected: %r" % (expected,))
    print("    actual:   %r" % (actual,))
    return ok


def run_sample_assertions():
    print("")
    print("=== step 2: delivered sample/boundary assertions ===")
    from locator import ModuleRange, SymbolRange, resolve_frame

    results = []

    # SAMPLE-1: run the CLI on samples/demo.json and inspect the report.
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "app.py"),
         os.path.join(ROOT, "samples", "demo.json"), "--json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        print("[FAIL] SAMPLE-0  app.py exited %d: %s" % (proc.returncode, proc.stderr))
        return False
    report = json.loads(proc.stdout)

    groups = report["groups"]
    results.append(check(
        "SAMPLE-1",
        "two diagnostics from different builds/load addresses form exactly one group",
        1,
        len(groups),
    ))
    member_ids = sorted(m["diagnostic_id"] for m in groups[0]["members"]) if groups else []
    results.append(check(
        "SAMPLE-2",
        "group members are diag-1 and diag-2",
        ["diag-1", "diag-2"],
        member_ids,
    ))
    key = (groups[0]["function"], groups[0]["source"]) if groups else None
    results.append(check(
        "SAMPLE-3",
        "group key is the first confirmable app frame parse_config @ src/config.c:42",
        ("parse_config", "src/config.c:42"),
        key,
    ))
    addrs = sorted(m["key_frame_address"] for m in groups[0]["members"]) if groups else []
    results.append(check(
        "SAMPLE-4",
        "the two grouped records keep their different absolute addresses",
        ["0x100000140", "0x200000140"],
        addrs,
    ))
    unresolved = {u["diagnostic_id"]: u["reason"] for u in report["unresolved"]}
    results.append(check(
        "SAMPLE-5",
        "diag-3 (build without symbol table) is listed separately as build_mismatch",
        "build_mismatch",
        unresolved.get("diag-3"),
    ))
    results.append(check(
        "SAMPLE-6",
        "diag-4 (no matching symbol) is listed separately as no_confirmable_app_frame",
        "no_confirmable_app_frame",
        unresolved.get("diag-4"),
    ))

    # BOUND-1/2: right endpoints are excluded from module/symbol ranges.
    mod = ModuleRange("app", 0x1000, 0x2000)
    results.append(check(
        "BOUND-1",
        "module range [0x1000, 0x2000): contains(0x1fff), not contains(0x2000)",
        (True, False),
        (mod.contains(0x1FFF), mod.contains(0x2000)),
    ))
    sym = SymbolRange(0x100, 0x200, "f", "f.c:1", True)
    results.append(check(
        "BOUND-2",
        "symbol range [0x100, 0x200): contains(0x1ff), not contains(0x200)",
        (True, False),
        (sym.contains(0x1FF), sym.contains(0x200)),
    ))

    # BOUND-3: overlapping module mappings keep the raw frame with a reason.
    modules = [ModuleRange("app", 0x1000, 0x2000), ModuleRange("plugin", 0x1800, 0x2800)]
    frame = resolve_frame(0x1900, modules, {"app": [], "plugin": []})
    results.append(check(
        "BOUND-3",
        "address inside two module ranges stays raw with reason overlapping_module_ranges",
        (False, "overlapping_module_ranges", "0x1900"),
        (frame["resolved"], frame["reason"], frame["address"]),
    ))

    return all(results)


def main():
    ok_suite = run_unittest_suite()
    ok_samples = run_sample_assertions()
    print("")
    if ok_suite and ok_samples:
        print("OVERALL: PASS")
        return 0
    print("OVERALL: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
