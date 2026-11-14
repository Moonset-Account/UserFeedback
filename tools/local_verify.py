#!/usr/bin/env python3
"""Local verification wrapper for the synthetic marker scanner.

Run with:  /usr/local/bin/python3 tools/local_verify.py

Steps:
  1. Discover the unittest suite via unittest.defaultTestLoader.discover("tests")
     and require at least one discovered case.
  2. Actually run the suite and require at least one test that really
     executed (not skipped); any failure/error/exception exits non-zero.
  3. Regenerate the delivered samples and run the CLI against them,
     asserting each documented sample/boundary expectation one by one,
     printing actual vs expected for every check.

Exit code 0 only if every step passes.
"""

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_unittest_suite():
    os.chdir(ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    count = suite.countTestCases()
    print("[verify] discovered %d test case(s)" % count)
    if count < 1:
        print("[verify] FAIL: zero tests discovered")
        return 1
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    skipped = len(result.skipped)
    executed = result.testsRun - skipped
    print("[verify] testsRun=%d skipped=%d executed=%d failures=%d errors=%d"
          % (result.testsRun, skipped, executed,
             len(result.failures), len(result.errors)))
    if executed < 1:
        print("[verify] FAIL: no tests actually executed (all skipped?)")
        return 1
    if not result.wasSuccessful():
        print("[verify] FAIL: unittest suite had failures/errors")
        return 1
    return 0


def run_sample_assertions():
    print("[verify] regenerating samples/")
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "make_samples.py")],
        capture_output=True, text=True)
    print(proc.stdout.strip())
    if proc.returncode != 0:
        print(proc.stderr)
        print("[verify] FAIL: sample generation failed")
        return 1

    cmd = [sys.executable, os.path.join(ROOT, "app.py"),
           os.path.join(ROOT, "samples", "build"),
           os.path.join(ROOT, "samples", "markers.txt"),
           "--chunk-size", "64", "--max-bytes", "100"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        print("[verify] FAIL: CLI did not emit JSON")
        print(proc.stdout)
        print(proc.stderr)
        return 1

    files = {f["path"]: f for f in report["files"]}
    failures = []

    def check(case_id, desc, actual, expected):
        ok = actual == expected
        if not ok:
            failures.append(case_id)
        print("[%s] %s %s | expected=%r actual=%r"
              % (case_id, "PASS" if ok else "FAIL", desc, expected, actual))

    check("SAMPLE-01", "exit code is 1 (markers found)", proc.returncode, 1)
    check("SAMPLE-02", "app.log: chunk-boundary hit of marker 1 on line 2",
          files.get("app.log", {}).get("matches"),
          [{"line": 2, "marker": 1}])
    check("SAMPLE-03", "app.log: status scanned",
          files.get("app.log", {}).get("status"), "scanned")
    check("SAMPLE-04", "server.log.gz: gzip hit of marker 2 on line 3",
          files.get("server.log.gz", {}).get("matches"),
          [{"line": 3, "marker": 2}])
    check("SAMPLE-05", "server.log.gz: flagged as compressed",
          files.get("server.log.gz", {}).get("compressed"), True)
    check("SAMPLE-06", "broken.gz: status corrupt",
          files.get("broken.gz", {}).get("status"), "corrupt")
    check("SAMPLE-07", "payload.bin: status skipped (binary)",
          files.get("payload.bin", {}).get("status"), "skipped")
    check("SAMPLE-08", "big.log: status truncated at 100-byte limit",
          (files.get("big.log", {}).get("status"),
           files.get("big.log", {}).get("bytes_scanned")),
          ("truncated", 100))
    check("SAMPLE-09", "big.log: marker past the limit not reported",
          files.get("big.log", {}).get("matches"), [])
    check("SAMPLE-10", "dead_link: status unreadable",
          files.get("dead_link", {}).get("status"), "unreadable")
    check("SAMPLE-11", "clean.txt: scanned with no matches",
          (files.get("clean.txt", {}).get("status"),
           files.get("clean.txt", {}).get("matches")),
          ("scanned", []))
    check("SAMPLE-12", "summary counts 2 marker hits",
          report.get("summary", {}).get("matches_total"), 2)
    check("SAMPLE-13", "result field is FAIL for CI",
          report.get("result"), "FAIL")
    marker_values = ["FAKE-TOKEN-ALPHA-7f3d9c", "FAKE-TOKEN-BETA-90ab12"]
    check("SAMPLE-14", "report never echoes marker values",
          any(v in proc.stdout for v in marker_values), False)

    if failures:
        print("[verify] FAIL: sample assertions failed: %s"
              % ", ".join(failures))
        return 1
    print("[verify] all sample assertions passed")
    return 0


def main():
    rc_tests = run_unittest_suite()
    rc_samples = run_sample_assertions()
    if rc_tests or rc_samples:
        print("[verify] OVERALL: FAIL")
        return 1
    print("[verify] OVERALL: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
