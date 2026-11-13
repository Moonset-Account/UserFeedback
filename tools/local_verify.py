#!/usr/bin/env python3
"""Local verification wrapper for SEC-VULN-050.

Runs the unittest suite discovered from tests/ (refusing to treat zero
discovered / zero actually executed tests as success), then re-runs the
delivered sample fixtures through app.py and checks each hand-computed
boundary assertion, printing actual vs expected for every check.

Run with: /usr/local/bin/python3 tools/local_verify.py
Exit code 0 only when the suite passes and every boundary assertion holds.
"""

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "fixtures")


def run_unittest_suite():
    suite = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests"))
    discovered = suite.countTestCases()
    print("[verify] discovered %d test case(s)" % discovered)
    if discovered < 1:
        print("[verify] FAIL: zero tests discovered")
        return False
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    executed = result.testsRun
    skipped = len(result.skipped)
    real = executed - skipped
    print("[verify] executed=%d skipped=%d actually_run=%d"
          % (executed, skipped, real))
    if real < 1:
        print("[verify] FAIL: no test actually executed (all skipped?)")
        return False
    if not result.wasSuccessful():
        print("[verify] FAIL: unittest suite had failures/errors")
        return False
    return True


def run_sample_report():
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "app.py"),
         "--cases", os.path.join(FIXTURES, "cases.json"),
         "--annotations", os.path.join(FIXTURES, "annotations.json"),
         "--v1", os.path.join(FIXTURES, "results_v1.json"),
         "--v2", os.path.join(FIXTURES, "results_v2.json")],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        print("[verify] FAIL: app.py exited %d: %s"
              % (proc.returncode, proc.stderr.strip()))
        return None
    return json.loads(proc.stdout)


def run_boundary_assertions(report):
    summary = report["summary"]
    fams = report["families"]
    cases = {c["case_id"]: c for c in report["cases"]}
    checks = [
        ("total_cases", summary["total_cases"], 6),
        ("annotated_cases", summary["annotated_cases"], 5),
        ("paired_evaluable_cases", summary["paired_evaluable_cases"], 4),
        ("undecidable_count", len(summary["undecidable_cases"]), 2),
        ("undecidable_reasons",
         [u["reason"] for u in summary["undecidable_cases"]],
         ["missing_annotation", "v2_status:timeout"]),
        ("v1_run_coverage", summary["run_coverage"]["v1"]["coverage"], 1.0),
        ("v2_run_coverage", summary["run_coverage"]["v2"]["coverage"],
         round(5 / 6, 4)),
        ("v1_totals", summary["totals"]["v1"],
         {"hits": 3, "misses": 1, "false_positives": 2}),
        ("v2_totals", summary["totals"]["v2"],
         {"hits": 3, "misses": 1, "false_positives": 0}),
        ("sqli_v2_hits", fams["sqli"]["v2"]["hits"], 2),
        ("sqli_new_hits", fams["sqli"]["new_hits"], 1),
        ("xss_lost_hits", fams["xss"]["lost_hits"], 1),
        ("cmdi_v1_fp_from_duplicate",
         fams["cmdi"]["v1"]["false_positives"], 1),
        ("C1_new_hits", cases["C1"]["new_hits"], ["A2"]),
        ("C3_lost_hits", cases["C3"]["lost_hits"], ["A3"]),
        ("C5_excluded_reason", cases["C5"]["reason"], "v2_status:timeout"),
        ("v1_mean_ms", summary["timing"]["v1"]["mean_ms"], 100.0),
        ("v2_mean_ms", summary["timing"]["v2"]["mean_ms"], 120.0),
        ("v2_timeouts", summary["timing"]["v2"]["timeouts"], 1),
    ]
    ok = True
    for name, actual, expected in checks:
        passed = actual == expected
        print("[verify] %-28s actual=%r expected=%r -> %s"
              % (name, actual, expected, "OK" if passed else "MISMATCH"))
        if not passed:
            ok = False
    return ok


def main():
    suite_ok = run_unittest_suite()
    report = run_sample_report()
    boundaries_ok = report is not None and run_boundary_assertions(report)
    if suite_ok and boundaries_ok:
        print("[verify] PASS")
        return 0
    print("[verify] FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
