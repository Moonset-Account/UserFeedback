#!/usr/bin/env python3
"""Local verification entry for SEC-VULN-036.

1. Discovers and runs the unittest suite via
   ``unittest.defaultTestLoader.discover("tests")``; fails (non-zero exit)
   when zero tests are discovered, all are skipped, or any test fails/errors.
2. Runs the delivered sample/boundary assertions end-to-end through the CLI
   and prints each actual result next to its expectation.

Run with: /usr/local/bin/python3 tools/local_verify.py
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_unit_tests():
    os.chdir(ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    count = suite.countTestCases()
    print("[verify] discovered %d test case(s)" % count)
    if count < 1:
        print("[verify] FAIL: zero tests discovered")
        return False
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    executed = result.testsRun - len(result.skipped)
    print("[verify] testsRun=%d skipped=%d executed=%d"
          % (result.testsRun, len(result.skipped), executed))
    if executed < 1:
        print("[verify] FAIL: no test actually executed (all skipped)")
        return False
    if not result.wasSuccessful():
        print("[verify] FAIL: unit tests had failures/errors")
        return False
    return True


def _check(checks, case_id, desc, actual, expected):
    checks.append((case_id, desc, actual, expected, actual == expected))


def run_sample_assertions():
    sys.path.insert(0, ROOT)
    import sample_fixtures as fx

    checks = []
    with tempfile.TemporaryDirectory() as td:
        # --- delivered sample: deletion + opaque dir + clean layer ----------
        layers = fx.build_sample_layers(td)
        rules = fx.write_rules(td)
        out = os.path.join(td, "report.json")
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "app.py"),
             "--layers"] + layers + ["--rules", rules, "--output", out],
            capture_output=True, text=True, timeout=60, cwd=ROOT)
        _check(checks, "S00", "sample CLI exit code", proc.returncode, 0)
        with open(out, encoding="utf-8") as fh:
            report = json.load(fh)
        s = report["summary"]
        _check(checks, "S01", "sample view_complete", report["view_complete"], True)
        _check(checks, "S02", "sample total_matches", s["total_matches"], 2)
        _check(checks, "S03", "sample visible/hidden", (s["visible"], s["hidden"]), (0, 2))
        _check(checks, "S04", "sample layers_with_matches", s["layers_with_matches"], [0])
        by_path = {m["path"]: m for m in report["matches"]}
        _check(checks, "S05", "whiteout-deleted marker not visible",
               by_path.get("app/config.env", {}).get("visible_final"), False)
        _check(checks, "S06", "opaque-hidden marker not visible",
               by_path.get("etc/secret.key", {}).get("visible_final"), False)
        fps_ok = all(m["fingerprint"].startswith("sha256:") for m in report["matches"])
        raw_absent = fx.TOKEN.decode() not in json.dumps(report)
        _check(checks, "S07", "fingerprints redacted, no raw marker in report",
               (fps_ok, raw_absent), (True, True))

        # --- boundary: corrupted later layer --------------------------------
        b0 = fx.write_layer(td, "b0.tar", [("secret.txt", fx.TOKEN)])
        b1 = fx.write_garbage_layer(td, "b1.tar")
        proc2 = subprocess.run(
            [sys.executable, os.path.join(ROOT, "app.py"),
             "--layers", b0, b1, "--rules", rules],
            capture_output=True, text=True, timeout=60, cwd=ROOT)
        _check(checks, "S08", "corrupted-layer CLI exit code", proc2.returncode, 0)
        rep2 = json.loads(proc2.stdout)
        _check(checks, "S09", "corrupted-layer view_complete",
               rep2["view_complete"], False)
        _check(checks, "S10", "corrupted-layer index", rep2["corrupted_layer"], 1)
        _check(checks, "S11", "history kept, visibility unknown",
               (rep2["summary"]["total_matches"], rep2["summary"]["unknown"]), (1, 1))

        # --- boundary: truncated layer --------------------------------------
        b2 = fx.write_truncated_layer(td, "b2.tar")
        proc3 = subprocess.run(
            [sys.executable, os.path.join(ROOT, "app.py"),
             "--layers", b0, b2, "--rules", rules],
            capture_output=True, text=True, timeout=60, cwd=ROOT)
        rep3 = json.loads(proc3.stdout)
        _check(checks, "S12", "truncated layer marked corrupted",
               rep3["layers"][1]["status"], "corrupted")

    ok_all = True
    for case_id, desc, actual, expected, ok in checks:
        print("[sample %s] %s: actual=%r expected=%r -> %s"
              % (case_id, desc, actual, expected, "PASS" if ok else "FAIL"))
        ok_all = ok_all and ok
    return ok_all


def main():
    try:
        ok = run_unit_tests()
        ok_samples = run_sample_assertions()
        ok = ok and ok_samples
    except Exception as exc:  # any wrapper error is a verification failure
        print("[verify] ERROR: %r" % (exc,))
        return 1
    print("[verify] overall: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
