#!/usr/bin/env python3
"""Local verification wrapper for SEC-20261114-053.

Steps:
  1. Discover the unittest suite under tests/ via
     unittest.defaultTestLoader.discover and require >= 1 discovered case.
  2. Actually run the suite; require >= 1 executed (non-skipped) test and a
     fully successful result.
  3. Run the delivered sample scenario (examples/) through app.py end to end
     and check each delivered sample/boundary assertion, printing the actual
     and expected value for every check.

Any discovery failure, zero executed tests, all-skipped suite, error,
failure, or sample mismatch exits non-zero.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_unit_suite():
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(
        str(ROOT / "tests"), top_level_dir=str(ROOT)
    )
    total = suite.countTestCases()
    print("[verify] discovered %d unit test(s)" % total)
    if total < 1:
        print("[verify] FAIL: zero tests discovered")
        return False
    runner = unittest.TextTestRunner(verbosity=1, stream=sys.stdout)
    result = runner.run(suite)
    executed = result.testsRun - len(result.skipped)
    print("[verify] ran=%d skipped=%d executed=%d"
          % (result.testsRun, len(result.skipped), executed))
    if executed < 1:
        print("[verify] FAIL: no test actually executed (zero or all skipped)")
        return False
    if not result.wasSuccessful():
        print("[verify] FAIL: unit suite had failures or errors")
        return False
    return True


def run_sample_checks():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "app.py"),
         "--input", str(ROOT / "examples" / "records.jsonl"),
         "--policy", str(ROOT / "examples" / "policy.json")],
        capture_output=True, text=True, timeout=30,
    )
    results = []

    def check(case_id, actual, expected):
        results.append((case_id, actual == expected, expected, actual))

    check("sample.exit_code", proc.returncode, 0)
    if proc.returncode != 0:
        print("[verify] app.py stderr: %s" % proc.stderr.strip())
    else:
        report = json.loads(proc.stdout)
        alerts = report["alerts"]
        quality = report["quality"]
        check("sample.alert_count", len(alerts), 1)
        if alerts:
            alert = alerts[0]
            check("sample.alert.service", alert["service"], "svc-a")
            check("sample.alert.window_start",
                  alert["window_start"], "2026-11-14T08:30:00Z")
            check("sample.alert.window_end",
                  alert["window_end"], "2026-11-14T08:40:00Z")
            check("sample.alert.samples", alert["samples"], 6)
            check("sample.alert.anomalous", alert["anomalous"], 2)
            check("sample.alert.anomalous_fingerprints",
                  alert["anomalous_fingerprints"],
                  [{"fingerprint": "fp-old", "count": 2}])
        check("sample.quality.parse_errors",
              len(quality["parse_error_lines"]), 1)
        check("sample.quality.missing_field_records",
              len(quality["missing_field_records"]), 1)
        check("sample.quality.conflicting_connections",
              len(quality["conflicting_connections"]), 1)
        check("sample.quality.unjudgeable_records",
              len(quality["unjudgeable_records"]), 1)
        check("sample.quality.failed_handshakes",
              quality["failed_handshakes"], 1)
        check("sample.quality.duplicate_reports",
              quality["duplicate_reports"], 1)
        windows = {(w["service"], w["window_start"]): w
                   for w in report["evaluated_windows"]}
        early = windows.get(("svc-a", "2026-11-14T08:00:00Z"))
        check("sample.window.0800.samples",
              early["samples"] if early else None, 5)
        check("sample.window.0800.anomalous",
              early["anomalous"] if early else None, 0)
        check("sample.window.0800.alerted",
              early["alerted"] if early else None, False)

    ok = True
    for case_id, passed, expected, actual in results:
        status = "PASS" if passed else "FAIL"
        print("[verify] %s %s expected=%r actual=%r"
              % (status, case_id, expected, actual))
        ok = ok and passed
    return ok


def main():
    ok_unit = run_unit_suite()
    ok_sample = run_sample_checks()
    if ok_unit and ok_sample:
        print("[verify] OK: unit suite and sample assertions passed")
        return 0
    print("[verify] FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
