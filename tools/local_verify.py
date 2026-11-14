#!/usr/bin/env python3
"""Local verification wrapper for the DMARC analyzer.

1. Discovers the unittest suite via unittest.defaultTestLoader.discover("tests")
   and actually runs it.  Zero discovered tests, all-skipped, errors or any
   failure exit non-zero -- unittest's "0 tests -> exit 0" is NOT a pass.
2. Runs the delivered sample fixtures (samples/) through app.py end to end
   and asserts the expected sample/boundary outcomes one by one, printing
   each actual result.

Usage: /usr/local/bin/python3 tools/local_verify.py
Exit code 0 only when every check passes.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILURES = []


def check(case_id, description, actual, expected):
    ok = actual == expected
    status = "PASS" if ok else "FAIL"
    print("[%s] %s\n       actual=%r expected=%r" % (status, case_id, actual, expected))
    if not ok:
        FAILURES.append(case_id)
    return ok


def run_unit_tests():
    print("== unit tests ==")
    suite = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests"))
    discovered = suite.countTestCases()
    print("discovered test cases: %d" % discovered)
    if discovered < 1:
        print("FAIL: no tests discovered")
        FAILURES.append("unit-discovery")
        return
    runner = unittest.TextTestRunner(verbosity=1, stream=sys.stdout)
    result = runner.run(suite)
    executed = result.testsRun - len(result.skipped)
    print("testsRun=%d skipped=%d executed=%d failures=%d errors=%d"
          % (result.testsRun, len(result.skipped), executed,
             len(result.failures), len(result.errors)))
    if executed < 1:
        print("FAIL: zero tests actually executed (all skipped?)")
        FAILURES.append("unit-executed")
    if result.failures or result.errors:
        print("FAIL: unit tests had failures/errors")
        FAILURES.append("unit-result")


def run_sample_assertions():
    print("== sample end-to-end assertions ==")
    samples = os.path.join(ROOT, "samples")
    registry = os.path.join(samples, "registry.csv")
    reports = [os.path.join(samples, name) for name in (
        "report_a.xml", "report_dup.xml", "report_b.xml",
        "report_conflict1.xml", "report_conflict2.xml")]
    with tempfile.TemporaryDirectory() as outdir:
        proc = subprocess.run(
            [sys.executable, "-I", os.path.join(ROOT, "app.py"),
             "--registry", registry, "--outdir", outdir] + reports,
            capture_output=True, text=True, cwd=ROOT, timeout=60)
        print("app.py exit=%d" % proc.returncode)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            print(proc.stderr)
            FAILURES.append("sample-run")
            return

        def load(name):
            with open(os.path.join(outdir, name), encoding="utf-8") as fh:
                return json.load(fh)

        reg = load("registered_sources.json")
        unk = load("unknown_sources.json")
        fwd = load("forwarding_risk.json")
        rev = load("review.json")

        # S01: identical resubmission (report_dup.xml) counted only once.
        check("S01-dedup-identical", "registered total counts report_a once",
              reg["total_count"], 7)
        # S02: registered rows keep disposition + spf/dkim auth+alignment.
        reg_rows = {(r["disposition"], r["compliant"]): r for r in reg["rows"]}
        check("S02-registered-rows", "two registered rows (none/quarantine)",
              len(reg["rows"]), 2)
        check("S03-registered-compliant",
              "registered none row: spf/dkim pass_aligned, compliant",
              (reg_rows[("none", True)]["count"],
               reg_rows[("none", True)]["spf"],
               reg_rows[("none", True)]["dkim"],
               reg_rows[("none", True)]["forwarded"]),
              (5, "pass_aligned", "pass_aligned", False))
        check("S04-registered-noncompliant",
              "registered quarantine row: neither mechanism passes",
              (reg_rows[("quarantine", False)]["count"],
               reg_rows[("quarantine", False)]["spf"],
               reg_rows[("quarantine", False)]["dkim"]),
              (2, "not_pass", "not_pass"))
        # S05: unknown source separated, forwarding marker set, dkim-only
        # compliance, disposition deviation from p=quarantine explained.
        check("S05-unknown-total", "unknown source total", unk["total_count"], 3)
        urow = unk["rows"][0]
        check("S06-unknown-row",
              "unknown row: forwarded, compliant via dkim, relies on forwarding",
              (urow["category"], urow["forwarded"], urow["compliant"],
               urow["relies_on_forwarding"], urow["disposition_deviation"],
               urow["spf"], urow["dkim"], urow["disposition"]),
              ("unknown", True, True, True, True,
               "not_pass", "pass_aligned", "none"))
        # S07: forwarding risk summary mirrors the forwarded row.
        check("S07-forwarding-risk", "forwarding risk total",
              fwd["total_count"], 3)
        # S08: conflicting resubmissions both go to review, excluded from counts.
        check("S08-review", "conflicting ReporterTwo:r2 pair in review",
              [(i["org"], i["report_id"], len(i["files"])) for i in rev["items"]],
              [("ReporterTwo", "r2", 2)])
        # S09: totals traceable to report IDs.
        check("S09-trace-registered", "registered total traceable to report IDs",
              reg["report_ids"], ["ReporterOne:r1"])
        check("S10-trace-unknown", "unknown total traceable to report IDs",
              unk["report_ids"], ["ReporterOne:r3"])
        check("S11-trace-forwarding", "forwarding risk traceable to report IDs",
              fwd["report_ids"], ["ReporterOne:r3"])
        # S12: date/domain/category dimensions present on every row.
        check("S12-dimensions", "row dimensions (domain,date,category)",
              sorted({(r["domain"], r["date"], r["category"])
                      for r in reg["rows"] + unk["rows"]}),
              [("example.com", "2026-11-13", "esp"),
               ("example.com", "2026-11-13", "unknown")])


def main():
    run_unit_tests()
    run_sample_assertions()
    print("== summary ==")
    if FAILURES:
        print("FAILED checks: %s" % ", ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
