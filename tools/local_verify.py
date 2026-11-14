#!/usr/bin/env python3
"""Local verification entry for SEC-20261114-057.

Runs from the repository root:
  /usr/local/bin/python3 tools/local_verify.py

Steps:
  1. Discover the unittest suite via unittest.defaultTestLoader.discover("tests")
     and require at least one test case.
  2. Actually run the suite; require at least one executed (non-skipped)
     test, no errors and no failures.
  3. Run the delivered reordering sample through app.py and check the
     sample/boundary assertions one by one, printing each actual result.

Any discovery of 0 tests, all-skipped suites, exceptions, failures or a
failed sample assertion exits non-zero.
"""

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")
SAMPLES = os.path.join(ROOT, "samples")


def run_unittest_suite():
    os.chdir(ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    count = suite.countTestCases()
    print(f"[unittest] discovered {count} test case(s)")
    if count < 1:
        print("[unittest] FAIL: zero tests discovered")
        return False
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    executed = result.testsRun - len(result.skipped)
    print(f"[unittest] ran={result.testsRun} skipped={len(result.skipped)} "
          f"executed={executed} failures={len(result.failures)} "
          f"errors={len(result.errors)}")
    if executed < 1:
        print("[unittest] FAIL: no test actually executed (all skipped?)")
        return False
    if not result.wasSuccessful():
        print("[unittest] FAIL: failures or errors present")
        return False
    print("[unittest] OK")
    return True


class SampleAssertions:
    def __init__(self):
        self.failures = 0
        self.total = 0

    def check(self, case_id, description, actual, expected):
        self.total += 1
        ok = actual == expected
        status = "PASS" if ok else "FAIL"
        print(f"[{case_id}] {status} {description}\n"
              f"    actual   = {actual!r}\n"
              f"    expected = {expected!r}")
        if not ok:
            self.failures += 1


def run_sample_assertions():
    """Execute the delivered reorder sample via the CLI and assert the
    reported sub-ranges, decisions and unsatisfied business ranges."""
    sa = SampleAssertions()
    proc = subprocess.run(
        [sys.executable, APP, "review",
         "--current", os.path.join(SAMPLES, "reorder_current.json"),
         "--candidate", os.path.join(SAMPLES, "reorder_candidate.json"),
         "--business", os.path.join(SAMPLES, "reorder_business.json"),
         "--format", "json"],
        capture_output=True, text=True, timeout=60)
    sa.check("sample.exit_code", "review exits 1 (unsatisfied ranges exist)",
             proc.returncode, 1)
    if proc.returncode not in (0, 1):
        print(proc.stderr)
        sa.check("sample.parseable", "stdout is parseable JSON", False, True)
        return sa.failures == 0
    try:
        report = json.loads(proc.stdout)
        sa.check("sample.parseable", "stdout is parseable JSON", True, True)
    except json.JSONDecodeError:
        sa.check("sample.parseable", "stdout is parseable JSON", False, True)
        return False

    changes = report["changes"]
    sa.check("sample.change_count",
             "two changed sub-ranges (office /16 minus ops /24)",
             len(changes), 2)
    flat = set()
    for c in changes:
        piece = c["src"]
        flat.update(piece if isinstance(piece, list) else [piece])
    sa.check("sample.change_cidrs",
             "changed source CIDRs = 10.20.0.0/16 - 10.20.7.0/24",
             flat, {"10.20.0.0/22", "10.20.4.0/23", "10.20.6.0/24",
                    "10.20.8.0/21", "10.20.16.0/20", "10.20.32.0/19",
                    "10.20.64.0/18", "10.20.128.0/17"})
    sa.check("sample.change_direction",
             "all changes flip allow -> deny",
             sorted({(c["before"]["action"], c["after"]["action"])
                     for c in changes}),
             [("allow", "deny")])
    sa.check("sample.change_rules",
             "before rule allow-all-internal, after rule deny-office-to-db",
             sorted({(c["before"]["rule"], c["after"]["rule"])
                     for c in changes}),
             [("allow-all-internal", "deny-office-to-db")])
    sa.check("sample.change_flow",
             "all changes are tcp/5432 towards 10.30.8.0/24",
             sorted({(c["dst"], c["proto"], c["port"]) for c in changes}),
             [("10.30.8.0/24", "tcp", "5432")])

    rule_only = report["rule_only_changes"]
    sa.check("sample.rule_only_count",
             "one rule-only change (ops /24 keeps allow, new matching rule)",
             len(rule_only), 1)
    if rule_only:
        sa.check("sample.rule_only_detail",
                 "ops /24 re-matched from allow-all-internal to allow-ops-to-db",
                 (rule_only[0]["src"], rule_only[0]["before"]["rule"],
                  rule_only[0]["after"]["rule"]),
                 ("10.20.7.0/24", "allow-all-internal", "allow-ops-to-db"))

    unsat = report["unsatisfied"]
    sa.check("sample.unsatisfied_count",
             "two unsatisfied business ranges", len(unsat), 2)
    by_biz = {u["business"]: u for u in unsat}
    office = by_biz.get("biz-office-to-db", {})
    sa.check("sample.unsatisfied_office",
             "office requirement only violated by the ops /24 exception",
             (office.get("src"), office.get("expected"), office.get("actual"),
              office.get("matched_rule")),
             ("10.20.7.0/24", "deny", "allow", "allow-ops-to-db"))
    partner = by_biz.get("biz-partner-to-db", {})
    sa.check("sample.unsatisfied_partner",
             "partner range still default-denied (no matching rule)",
             (partner.get("src"), partner.get("expected"),
              partner.get("actual"), partner.get("matched_rule")),
             ("172.16.9.0/24", "allow", "deny", None))

    print(f"[sample] {sa.total - sa.failures}/{sa.total} assertions passed")
    return sa.failures == 0


def main():
    ok_tests = run_unittest_suite()
    ok_sample = run_sample_assertions()
    if ok_tests and ok_sample:
        print("LOCAL VERIFY: PASS")
        return 0
    print("LOCAL VERIFY: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
