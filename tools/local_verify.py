#!/usr/bin/env python3
"""Local verification wrapper for the patch coverage matrix deliverable.

Steps:
  1. Discover the unittest suite via unittest.defaultTestLoader.discover("tests")
     and require countTestCases() >= 1.
  2. Actually run the suite and require at least 1 executed, non-skipped
     test; zero discovered tests, all-skipped runs, errors or failures all
     exit non-zero (a zero-test unittest run exiting 0 is NOT a pass).
  3. Run the delivered sample/boundary assertions against app.py and keep
     each actual result next to its expected value, one line per check.

Usage:  /usr/local/bin/python3 tools/local_verify.py
Exit:   0 only when the unit suite and every sample/boundary check pass.
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


def run_unit_suite() -> bool:
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))  # make the covmatrix package importable
    suite = unittest.defaultTestLoader.discover("tests")
    count = suite.countTestCases()
    print(f"[verify] discovered {count} unit test case(s)")
    if count < 1:
        print("[verify] FAIL: zero tests discovered; refusing to treat as pass")
        return False
    runner = unittest.TextTestRunner(verbosity=1, stream=sys.stdout)
    result = runner.run(suite)
    executed = result.testsRun - len(result.skipped)
    print(
        f"[verify] ran {result.testsRun} test(s): {executed} executed non-skipped, "
        f"{len(result.skipped)} skipped, {len(result.failures)} failure(s), "
        f"{len(result.errors)} error(s)"
    )
    if executed < 1:
        print("[verify] FAIL: no test actually executed (all skipped?)")
        return False
    if not result.wasSuccessful():
        print("[verify] FAIL: unit suite had failures or errors")
        return False
    return True


def run_cli(*extra):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "app.py"),
            "--entries", str(SAMPLES / "entries.json"),
            "--graph", str(SAMPLES / "graph.json"),
            "--checkpoints", str(SAMPLES / "checkpoints.json"),
            "--traces", str(SAMPLES / "traces.json"),
            *extra,
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )


def entry_by_id(report, eid):
    return next(e for e in report["entries"] if e["id"] == eid)


def run_sample_checks() -> bool:
    """Execute the delivered sample/boundary assertions one by one."""
    proc = run_cli("--format", "json")
    checks = []  # (case_id, description, actual, expected)

    if proc.returncode != 0:
        print(f"[verify] FAIL: sample CLI run exited {proc.returncode}: {proc.stderr}")
        return False
    report = json.loads(proc.stdout)

    statuses = {e["id"]: e["status"] for e in report["entries"]}
    checks.append(("SM-01", "E1 status (T1 covers CP1+CP2 of build B2026-11-14)",
                   statuses.get("E1"), "observed_covered"))
    checks.append(("SM-02", "E2 status (T2 recorded against old build B2026-11-13)",
                   statuses.get("E2"), "unattributable"))
    checks.append(("SM-03", "E3 status (T5 covers CP3)",
                   statuses.get("E3"), "observed_covered"))
    checks.append(("SM-04", "E4 status (T4 claims CP1, unreachable from admin.console)",
                   statuses.get("E4"), "unattributable"))
    checks.append(("SM-05", "E5 status (report.gen reaches no checkpoint)",
                   statuses.get("E5"), "no_reachable_checkpoint"))
    checks.append(("SM-06", "E6 status (reachable from store.cache, never tested)",
                   statuses.get("E6"), "statically_reachable"))
    checks.append(("SM-07", "one test covering two checkpoints recorded separately",
                   entry_by_id(report, "E1")["coverage_records"],
                   [{"test_id": "T1", "checkpoint_id": "CP1"},
                    {"test_id": "T1", "checkpoint_id": "CP2"}]))
    checks.append(("SM-08", "E1 missing checkpoint (reachable but unobserved)",
                   entry_by_id(report, "E1")["missing_checkpoints"], ["CP3"]))
    checks.append(("SM-09", "E2 nearest observed checkpoint (same module, distance 0)",
                   entry_by_id(report, "E2")["nearest_observed_checkpoint"],
                   {"id": "CP1", "module": "parser.shared", "distance": 0,
                    "path": ["parser.shared"]}))
    checks.append(("SM-10", "E6 nearest observed checkpoint (one hop away)",
                   entry_by_id(report, "E6")["nearest_observed_checkpoint"],
                   {"id": "CP1", "module": "parser.shared", "distance": 1,
                    "path": ["store.cache", "parser.shared"]}))
    checks.append(("SM-11", "E4 nearest observed checkpoint (nothing reachable)",
                   entry_by_id(report, "E4")["nearest_observed_checkpoint"], None))
    checks.append(("SM-12", "uncovered entries reported for regression planning",
                   report["uncovered_entries"], ["E2", "E4", "E6"]))
    checks.append(("SM-13", "trace with unknown entry E99 is globally unattributable",
                   [(r["test_id"], r["reason"]) for r in report["unattributable_traces"]],
                   [("T3", "unknown_entry")]))
    checks.append(("SM-14", "E2 invalid trace reason (build must match)",
                   entry_by_id(report, "E2")["invalid_traces"][0]["reason"],
                   "build_mismatch"))
    checks.append(("SM-15", "E1 invalid trace reason (CP9 not part of patch)",
                   entry_by_id(report, "E1")["invalid_traces"][0]["reason"],
                   "unknown_checkpoint"))
    checks.append(("SM-16", "E4 invalid trace reason (checkpoint not reachable)",
                   entry_by_id(report, "E4")["invalid_traces"][0]["reason"],
                   "checkpoint_not_reachable"))

    # Boundary assertions on CLI behaviour.
    boundary = run_cli("--fail-on-uncovered")
    checks.append(("SM-17", "--fail-on-uncovered exit code while E2/E4/E6 uncovered",
                   boundary.returncode, 1))
    missing = subprocess.run(
        [
            sys.executable,
            str(ROOT / "app.py"),
            "--entries", str(SAMPLES / "does-not-exist.json"),
            "--graph", str(SAMPLES / "graph.json"),
            "--checkpoints", str(SAMPLES / "checkpoints.json"),
            "--traces", str(SAMPLES / "traces.json"),
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    checks.append(("SM-18", "missing input file exit code", missing.returncode, 2))
    checks.append(("SM-19", "missing input file reports an error on stderr",
                   missing.stderr.startswith("error:"), True))

    ok = True
    for case_id, description, actual, expected in checks:
        passed = actual == expected
        ok = ok and passed
        verdict = "PASS" if passed else "FAIL"
        print(f"[verify] {case_id} {verdict}: {description}")
        print(f"         actual   = {actual!r}")
        print(f"         expected = {expected!r}")
    return ok


def main() -> int:
    print("[verify] step 1/2: unit test suite")
    unit_ok = run_unit_suite()
    print("[verify] step 2/2: delivered sample/boundary assertions")
    sample_ok = run_sample_checks()
    if unit_ok and sample_ok:
        print("[verify] RESULT: PASS")
        return 0
    print("[verify] RESULT: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
