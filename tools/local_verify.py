#!/usr/bin/env python3
"""Local verification wrapper for the contacts importer task.

Runs, in order:

1. the unittest property-test suite discovered from ``tests/`` (fails if
   zero tests are discovered, if every test is skipped, or on any
   failure/error);
2. the delivered sample assertions: every file in ``samples/`` is parsed
   through ``app.py`` and its JSON output is compared against the
   hand-written expectation in ``expectations.py``;
3. boundary assertions: chunk-size-1 parsing must equal whole-buffer
   parsing, and the empty input must yield an empty result.

Every check prints its own PASS/FAIL line with the actual observed
result.  Exit code is 0 only if all checks pass.
"""

import io
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = "/usr/local/bin/python3" if os.path.exists("/usr/local/bin/python3") else sys.executable

sys.path.insert(0, ROOT)


def run_unittest_suite():
    os.chdir(ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    total = suite.countTestCases()
    print("[verify] discovered %d unittest case(s) in tests/" % total, flush=True)
    if total < 1:
        print("[verify] FAIL: zero tests discovered")
        return False
    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=1)
    result = runner.run(suite)
    sys.stdout.write(stream.getvalue())
    sys.stdout.flush()
    skipped = len(result.skipped)
    executed = result.testsRun - skipped
    print(
        "[verify] unittest summary: testsRun=%d skipped=%d executed=%d "
        "failures=%d errors=%d"
        % (result.testsRun, skipped, executed, len(result.failures), len(result.errors)),
        flush=True,
    )
    if executed < 1:
        print("[verify] FAIL: no test was actually executed (all skipped?)")
        return False
    if not result.wasSuccessful():
        print("[verify] FAIL: unittest suite reported failures/errors")
        return False
    print("[verify] PASS: unittest suite (%d executed)" % executed)
    return True


def run_app(args, timeout=30):
    return subprocess.run(
        [PY, os.path.join(ROOT, "app.py")] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=ROOT,
    )


def project(payload):
    return {
        "contacts": payload["contacts"],
        "errors": [
            {"record": e["record"], "category": e["category"]}
            for e in payload["errors"]
        ],
    }


def check(label, ok, actual):
    status = "PASS" if ok else "FAIL"
    print("[sample %s] %s actual=%s" % (label, status, json.dumps(actual, ensure_ascii=False, sort_keys=True)), flush=True)
    return ok


def run_sample_assertions():
    from expectations import EXPECTED_SIGNATURES

    ok = True
    for case_id in sorted(EXPECTED_SIGNATURES):
        expected = EXPECTED_SIGNATURES[case_id]
        proc = run_app([os.path.join("samples", case_id + ".csv")])
        if proc.returncode != 0:
            ok = check(case_id, False, {"exit": proc.returncode, "stderr": proc.stderr.strip()}) and ok
            continue
        try:
            actual = project(json.loads(proc.stdout))
        except (ValueError, KeyError) as exc:
            ok = check(case_id, False, {"unparsable_output": str(exc), "stdout": proc.stdout[:200]}) and ok
            continue
        ok = check(case_id, actual == expected, actual) and ok
        if actual != expected:
            print("    expected=%s" % json.dumps(expected, ensure_ascii=False, sort_keys=True), flush=True)
    return ok


def run_boundary_assertions():
    ok = True

    # chunk-size 1 must equal chunk-size 65536 on a quoted, multiline sample
    for case_id in ("valid_quoted", "mixed_valid_invalid", "invalid_unterminated"):
        one = run_app(["--chunk-size", "1", os.path.join("samples", case_id + ".csv")])
        bulk = run_app(["--chunk-size", "65536", os.path.join("samples", case_id + ".csv")])
        same = one.returncode == bulk.returncode == 0 and one.stdout == bulk.stdout
        ok = check("boundary/chunk1-vs-bulk/%s" % case_id, same,
                   {"chunk1_exit": one.returncode, "bulk_exit": bulk.returncode,
                    "outputs_equal": one.stdout == bulk.stdout}) and ok

    # empty input -> empty result, exit 0
    proc = run_app([os.path.join("samples", "empty.csv")])
    try:
        payload = json.loads(proc.stdout)
        empty_ok = proc.returncode == 0 and payload == {"contacts": [], "errors": []}
    except ValueError:
        empty_ok = False
    ok = check("boundary/empty-input", empty_ok,
               {"exit": proc.returncode, "stdout": proc.stdout.strip()[:120]}) and ok

    # stdin delivery equals file delivery
    with open(os.path.join(ROOT, "samples", "valid_basic.csv"), "r", encoding="utf-8") as fh:
        proc_stdin = subprocess.run(
            [PY, os.path.join(ROOT, "app.py"), "-"],
            stdin=fh, capture_output=True, text=True, timeout=30, cwd=ROOT,
        )
    proc_file = run_app([os.path.join("samples", "valid_basic.csv")])
    ok = check("boundary/stdin-vs-file", proc_stdin.stdout == proc_file.stdout and proc_stdin.returncode == 0,
               {"stdin_exit": proc_stdin.returncode}) and ok
    return ok


def main():
    print("[verify] python: %s" % PY, flush=True)
    ok_tests = run_unittest_suite()
    ok_samples = run_sample_assertions()
    ok_boundary = run_boundary_assertions()
    print(
        "[verify] overall: unittests=%s samples=%s boundary=%s"
        % ("PASS" if ok_tests else "FAIL",
           "PASS" if ok_samples else "FAIL",
           "PASS" if ok_boundary else "FAIL"),
        flush=True,
    )
    if ok_tests and ok_samples and ok_boundary:
        print("[verify] RESULT: PASS", flush=True)
        return 0
    print("[verify] RESULT: FAIL", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
