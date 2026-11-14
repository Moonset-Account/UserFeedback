#!/usr/bin/env python3
"""Local verification entry for the firmware layout checker.

Runs, in order:

  1. the unittest suite discovered from tests/ (must contain and
     actually execute at least one non-skipped test, with no
     failures/errors);
  2. the delivered sample/boundary assertions (SA-*/SB-* case ids)
     against samples/demo.bin and a synthetic truncated image, printing
     each case's actual vs expected result.

Exit code 0 only if every step passes; any zero-test run, all-skipped
run, failure, error or failed sample assertion exits non-zero.

Run with:  /usr/local/bin/python3 tools/local_verify.py
"""

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")
BOARD = os.path.join(ROOT, "samples", "board.json")
DEMO = os.path.join(ROOT, "samples", "demo.bin")


def run_unit_tests():
    os.chdir(ROOT)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    total = suite.countTestCases()
    print("[verify] discovered %d test case(s)" % total)
    if total < 1:
        print("[verify] FAIL: zero tests discovered")
        return False
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    skipped = len(result.skipped)
    executed = result.testsRun - skipped
    print("[verify] testsRun=%d skipped=%d executed=%d failures=%d errors=%d"
          % (result.testsRun, skipped, executed,
             len(result.failures), len(result.errors)))
    if executed < 1:
        print("[verify] FAIL: zero tests actually executed "
              "(discovered tests were all skipped)")
        return False
    if not result.wasSuccessful():
        print("[verify] FAIL: unit tests had failures/errors")
        return False
    print("[verify] unit tests OK")
    return True


def run_cli(args):
    return subprocess.run(
        [sys.executable, APP] + args,
        capture_output=True, text=True, timeout=30, cwd=ROOT,
    )


def sample_assertions():
    ok = True

    def check(case_id, actual, expected):
        nonlocal ok
        good = actual == expected
        if not good:
            ok = False
        print("[case %s] %s actual=%r expected=%r"
              % (case_id, "PASS" if good else "FAIL", actual, expected))

    if not os.path.exists(DEMO):
        print("[verify] FAIL: %s missing (run tools/build_sample.py)" % DEMO)
        return False

    proc = run_cli([BOARD, DEMO])
    if proc.returncode != 0:
        print("[verify] FAIL: CLI exited %d: %s"
              % (proc.returncode, proc.stderr.strip()))
        return False
    report = json.loads(proc.stdout)
    image = report["images"][0]
    findings = image["findings"]
    overlaps = [f for f in findings if f["type"] == "overlap"]

    check("SA-STATUS-CHECKED", image["status"], "checked")
    check("SA-HEADER",
          (image["header"]["record_count"], image["header"]["header_size"],
           image["header"]["record_size"]),
          (5, 88, 16))
    check("SA-OVERLAP-PAIRS",
          sorted(tuple(f["record_offsets"]) for f in overlaps),
          [(24, 40)])
    check("SA-OVERLAP-RANGE",
          overlaps[0]["overlap_range"] if overlaps else None,
          [700, 768])
    # P0 [256,512) and P1 [512,768) are adjacent: record offset 8 must
    # not appear in any overlap finding.
    check("SA-ADJACENT-NOT-OVERLAP",
          any(8 in f["record_offsets"] for f in overlaps),
          False)
    check("SA-OUT-OF-IMAGE",
          [f["record_offset"] for f in findings
           if f["type"] == "out_of_image"],
          [56])
    check("SA-RESERVED-REGION",
          [(f["record_offset"], f["region"]) for f in findings
           if f["type"] == "reserved_region"],
          [(72, "bootrom")])

    # Boundary case through the CLI: declared count 3 but the image
    # holds only 2 full records + 6 bytes -> truncated record @40.
    tmp = os.path.join(ROOT, "tests", ".verify_truncated.bin")
    try:
        with open(tmp, "wb") as fh:
            fh.write(bytes.fromhex("46574c47") + (3).to_bytes(4, "little")
                     + bytes(2 * 16 + 6))
        proc2 = run_cli([BOARD, tmp])
        if proc2.returncode != 0:
            print("[verify] FAIL: CLI exited %d on truncated fixture"
                  % proc2.returncode)
            return False
        rep2 = json.loads(proc2.stdout)
        trunc = [(f["record_index"], f["record_offset"])
                 for f in rep2["images"][0]["findings"]
                 if f["type"] == "truncated_record"]
        check("SB-TRUNCATED-RECORD", trunc, [(2, 40)])
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    return ok


def main():
    tests_ok = run_unit_tests()
    samples_ok = sample_assertions()
    if tests_ok and samples_ok:
        print("[verify] OVERALL: PASS")
        return 0
    print("[verify] OVERALL: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
