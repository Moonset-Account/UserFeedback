#!/usr/bin/env python3
"""Local verification entry for the workspace lock scheduler.

Runs (must ALL pass, otherwise exits non-zero):
1. unittest discovery over tests/ — requires >= 1 discovered case, actually
   runs the suite, and requires >= 1 executed (non-skipped) test with no
   failures/errors. Zero discovered / all-skipped / error / failure => exit 1.
2. Delivered sample assertions: every samples/*.json (excluding *.expected.json)
   is executed through app.py and the actual report is compared field-by-field
   against samples/<name>.expected.json. Each case result is printed and kept.

Usage: /usr/local/bin/python3 tools/local_verify.py
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_unit_tests() -> bool:
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    count = suite.countTestCases()
    print(f"[verify] discovered {count} test case(s) in tests/")
    if count < 1:
        print("[verify] FAIL: zero tests discovered")
        return False
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    executed = result.testsRun - len(result.skipped)
    print(f"[verify] testsRun={result.testsRun} skipped={len(result.skipped)} executed={executed}")
    if executed < 1:
        print("[verify] FAIL: zero tests actually executed (all skipped?)")
        return False
    if not result.wasSuccessful():
        print("[verify] FAIL: unit tests had failures or errors")
        return False
    print("[verify] unit tests: PASS")
    return True


def _diff(prefix, actual, expected, out):
    if isinstance(actual, dict) and isinstance(expected, dict):
        for key in sorted(set(actual) | set(expected)):
            if key not in actual:
                out.append(f"{prefix}.{key}: missing in actual (expected {expected[key]!r})")
            elif key not in expected:
                out.append(f"{prefix}.{key}: unexpected in actual ({actual[key]!r})")
            else:
                _diff(f"{prefix}.{key}", actual[key], expected[key], out)
    elif isinstance(actual, list) and isinstance(expected, list):
        if len(actual) != len(expected):
            out.append(f"{prefix}: length {len(actual)} != expected {len(expected)}")
        for i, (a, e) in enumerate(zip(actual, expected)):
            _diff(f"{prefix}[{i}]", a, e, out)
    elif actual != expected:
        out.append(f"{prefix}: actual {actual!r} != expected {expected!r}")


def run_samples() -> bool:
    ok = True
    cases = sorted(
        p for p in (ROOT / "samples").glob("*.json")
        if not p.name.endswith(".expected.json")
    )
    if not cases:
        print("[verify] FAIL: no sample cases found")
        return False
    for case_path in cases:
        case_id = case_path.stem
        expected_path = case_path.with_name(case_path.stem + ".expected.json")
        proc = subprocess.run(
            [sys.executable, str(ROOT / "app.py"), str(case_path), "--compact"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            print(f"[sample {case_id}] FAIL: app.py exited {proc.returncode}: {proc.stderr.strip()}")
            ok = False
            continue
        actual = json.loads(proc.stdout)
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        if actual == expected:
            ws_bits = "; ".join(
                f"{w['workspace_id']} locked={w['locked']} "
                f"next={w['next_transition_utc']}"
                for w in actual["workspaces"]
            )
            print(f"[sample {case_id}] PASS ({ws_bits})")
        else:
            diffs = []
            _diff("report", actual, expected, diffs)
            print(f"[sample {case_id}] FAIL:")
            for line in diffs:
                print(f"    {line}")
            ok = False
    return ok


def main() -> int:
    ok_tests = run_unit_tests()
    ok_samples = run_samples()
    if ok_tests and ok_samples:
        print("[verify] OVERALL: PASS")
        return 0
    print("[verify] OVERALL: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
