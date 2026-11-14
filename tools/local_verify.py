#!/usr/bin/env python3
"""Local verification entry point.

Responsibilities (see local delivery contract):

1. Discover the suite with ``unittest.defaultTestLoader.discover("tests")`` and
   require ``countTestCases() >= 1`` before running anything.
2. Run the suite and require at least one test that actually executed and was
   not skipped.  Any failure, error, empty discovery or all-skipped run exits
   non-zero -- unittest's own exit code for an empty discovery (0) is not
   accepted as success.
3. Re-run a set of delivered sample / boundary assertions end-to-end through
   the real ``triage`` package, recording the actual result of every check.

Standard library only.  Run with::

    /usr/local/bin/python3 tools/local_verify.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
os.chdir(WORKSPACE)
sys.path.insert(0, str(WORKSPACE))

from triage.engine import (  # noqa: E402
    REASON_DEBUG_ID_MISMATCH,
    REASON_SYMBOL_CONFLICT,
    REASON_UNSAFE_PATH,
    triage,
)
from triage.models import load_index  # noqa: E402


def _write_build(
    base: Path,
    build_id: str,
    architecture: str,
    artifacts: list[dict],
    files: dict[str, bytes],
) -> tuple[Path, Path]:
    root = base / f"root-{build_id}-{architecture}"
    root.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    index = base / f"index-{build_id}.json"
    index.write_text(
        json.dumps(
            {
                "build_id": build_id,
                "architecture": architecture,
                "artifacts": artifacts,
            }
        ),
        encoding="utf-8",
    )
    return root, index


def _sample_assertions() -> list[dict]:
    """Delivered sample checks, each with stable case_id and expected value."""

    checks: list[dict] = []
    base = Path(tempfile.mkdtemp(prefix="local-verify-"))

    # --- S-001: public plan contains runtime files only ----------------
    debug_id = "S001-ID-AAAA"
    root, index = _write_build(
        base,
        "S001",
        "arm64",
        [
            {"role": "main_program", "path": "bin/app", "debug_id": debug_id},
            {"role": "runtime_library", "path": "lib/runtime.dylib"},
            {"role": "debug_symbol", "path": "symbols/app.dbg",
             "debug_id": debug_id},
        ],
        {
            "bin/app": b"app-binary",
            "lib/runtime.dylib": b"runtime",
            "symbols/app.dbg": b"debug-data",
        },
    )
    result = triage([load_index(index, root)])
    actual_roles = sorted(
        entry["role"]
        for batch in result.public_plan["batches"]
        for entry in batch["files"]
    )
    checks.append({
        "case_id": "S-001",
        "claim": "public archive plan lists runtime files only",
        "input": "one arm64 build with main_program, runtime_library, "
                 "debug_symbol",
        "expected": ["main_program", "runtime_library"],
        "actual": actual_roles,
        "passed": actual_roles == ["main_program", "runtime_library"],
    })

    # --- S-002: identical symbols shared by two builds merge -----------
    shared = b"identical DWARF payload"
    builds = []
    for build_id in ("S002A", "S002B"):
        r, i = _write_build(
            base,
            build_id,
            "arm64",
            [
                {"role": "main_program", "path": "bin/app",
                 "debug_id": "S002-MERGE"},
                {"role": "debug_symbol", "path": "symbols/app.dbg",
                 "debug_id": "S002-MERGE"},
            ],
            {
                "bin/app": f"bin-{build_id}".encode(),
                "symbols/app.dbg": shared,
            },
        )
        builds.append(load_index(i, r))
    result = triage(builds)
    entry = result.internal_plan["architectures"][0]["debug_ids"]["S002-MERGE"]
    actual_merge = {
        "merged": entry["merged"],
        "reference_count": entry["reference_count"],
    }
    checks.append({
        "case_id": "S-002",
        "claim": "same architecture+debug_id with identical content merges",
        "input": "two arm64 builds, one shared debug id, equal symbol bytes",
        "expected": {"merged": True, "reference_count": 2},
        "actual": actual_merge,
        "passed": actual_merge
        == {"merged": True, "reference_count": 2},
    })

    # --- S-003: same debug id, different content -> conflict ------------
    builds = []
    for build_id, payload in (
        ("S003A", b"symbol payload one"),
        ("S003B", b"symbol payload two -- different"),
    ):
        r, i = _write_build(
            base,
            build_id,
            "arm64",
            [
                {"role": "main_program", "path": "bin/app",
                 "debug_id": "S003-CONFLICT"},
                {"role": "debug_symbol", "path": "symbols/app.dbg",
                 "debug_id": "S003-CONFLICT"},
            ],
            {"bin/app": b"bin", "symbols/app.dbg": payload},
        )
        builds.append(load_index(i, r))
    result = triage(builds)
    actual_conflict = sorted(
        reason["code"]
        for evaluated in result.blocked_batches
        for reason in evaluated.reasons
    )
    checks.append({
        "case_id": "S-003",
        "claim": "same debug id with different content blocks both batches",
        "input": "two arm64 builds sharing debug id S003-CONFLICT, "
                 "different symbol bytes",
        "expected": [REASON_SYMBOL_CONFLICT, REASON_SYMBOL_CONFLICT],
        "actual": actual_conflict,
        "passed": actual_conflict
        == [REASON_SYMBOL_CONFLICT, REASON_SYMBOL_CONFLICT],
    })

    # --- S-004: debug id mismatch blocks only the affected batch --------
    r1, i1 = _write_build(
        base, "S004-GOOD", "arm64",
        [
            {"role": "main_program", "path": "bin/app", "debug_id": "GOOD"},
            {"role": "debug_symbol", "path": "symbols/a.dbg",
             "debug_id": "GOOD"},
        ],
        {"bin/app": b"a", "symbols/a.dbg": b"b"},
    )
    r2, i2 = _write_build(
        base, "S004-BAD", "arm64",
        [
            {"role": "main_program", "path": "bin/app",
             "debug_id": "BAD-MAIN"},
            {"role": "debug_symbol", "path": "symbols/a.dbg",
             "debug_id": "BAD-SYMBOL"},
        ],
        {"bin/app": b"c", "symbols/a.dbg": b"d"},
    )
    result = triage([load_index(i1, r1), load_index(i2, r2)])
    blocked_ids = [b.build.build_id for b in result.blocked_batches]
    mismatch_codes = {
        reason["code"]
        for evaluated in result.blocked_batches
        for reason in evaluated.reasons
    }
    publishable_ids = [
        batch["build_id"]
        for batch in result.block_manifest["publishable"]["batches"]
    ]
    actual_s004 = {
        "blocked": blocked_ids,
        "reason_in_blocked": REASON_DEBUG_ID_MISMATCH in mismatch_codes,
        "publishable": publishable_ids,
    }
    checks.append({
        "case_id": "S-004",
        "claim": "mismatched debug identifiers hold only the affected batch",
        "input": "one healthy build plus one build whose main/symbol ids differ",
        "expected": {
            "blocked": ["S004-BAD"],
            "reason_in_blocked": True,
            "publishable": ["S004-GOOD"],
        },
        "actual": actual_s004,
        "passed": actual_s004 == {
            "blocked": ["S004-BAD"],
            "reason_in_blocked": True,
            "publishable": ["S004-GOOD"],
        },
    })

    # --- S-005: parent traversal is rejected -----------------------------
    (base / "outside.txt").write_bytes(b"secret")
    r, i = _write_build(
        base, "S005", "arm64",
        [
            {"role": "main_program", "path": "../outside.txt",
             "debug_id": "S005"},
        ],
        {},
    )
    result = triage([load_index(i, r)])
    actual_s005 = sorted(
        reason["code"]
        for evaluated in result.blocked_batches
        for reason in evaluated.reasons
    )
    # The unsafe path cannot become a main program, so the batch also fails
    # the main-program requirement; the security reason is the decisive one.
    expected_s005 = ["missing_main_program", REASON_UNSAFE_PATH]
    checks.append({
        "case_id": "S-005",
        "claim": "artifact paths containing '..' cannot escape the root",
        "input": "index declaring main_program at ../outside.txt",
        "expected": expected_s005,
        "actual": actual_s005,
        "passed": actual_s005 == expected_s005,
    })
    return checks


def main() -> int:
    started = time.monotonic()
    report: dict = {
        "workspace": str(WORKSPACE),
        "python": sys.version,
        "discovery": {},
        "unit_tests": {},
        "sample_assertions": [],
    }

    loader = unittest.defaultTestLoader
    suite = loader.discover("tests")
    discovered = suite.countTestCases()
    report["discovery"] = {
        "method": "unittest.defaultTestLoader.discover('tests')",
        "discovered_test_count": discovered,
    }
    if discovered < 1:
        report["status"] = "failed"
        report["failure"] = "test discovery returned 0 cases"
        _emit(report, started)
        return 2

    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2)
    run_result = runner.run(suite)
    executed = run_result.testsRun
    skipped = len(run_result.skipped)
    really_ran = executed - skipped
    report["unit_tests"] = {
        "tests_run": executed,
        "skipped": skipped,
        "executed_not_skipped": really_ran,
        "failures": len(run_result.failures),
        "errors": len(run_result.errors),
        "output": stream.getvalue(),
    }

    samples = _sample_assertions()
    report["sample_assertions"] = samples
    samples_passed = all(item["passed"] for item in samples)

    unit_ok = (
        really_ran >= 1
        and not run_result.failures
        and not run_result.errors
    )
    report["sample_summary"] = {
        "total": len(samples),
        "passed": sum(1 for item in samples if item["passed"]),
        "failed": sum(1 for item in samples if not item["passed"]),
    }
    report["status"] = (
        "passed" if unit_ok and samples_passed else "failed"
    )
    exit_code = 0 if report["status"] == "passed" else 1
    _emit(report, started)
    return exit_code


def _emit(report: dict, started: float) -> None:
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    verify_dir = WORKSPACE / "verify"
    verify_dir.mkdir(parents=True, exist_ok=True)
    (verify_dir / "local_verify_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    lines = [
        f"local_verify status: {report['status']}",
        f"discovered tests   : {report['discovery']['discovered_test_count']}",
        f"tests run/skipped  : "
        f"{report['unit_tests'].get('tests_run', 0)}/"
        f"{report['unit_tests'].get('skipped', 0)}",
        f"failures/errors    : "
        f"{report['unit_tests'].get('failures', 0)}/"
        f"{report['unit_tests'].get('errors', 0)}",
        f"sample assertions  : "
        f"{report.get('sample_summary', {}).get('passed', 0)}/"
        f"{report.get('sample_summary', {}).get('total', 0)} passed",
        "",
        "sample details:",
    ]
    for item in report["sample_assertions"]:
        lines.append(
            f"  [{'PASS' if item['passed'] else 'FAIL'}] {item['case_id']} "
            f"{item['claim']}"
        )
        lines.append(f"      expected: {item['expected']}")
        lines.append(f"      actual  : {item['actual']}")
    text = "\n".join(lines) + "\n"
    (verify_dir / "local_verify_report.txt").write_text(
        text, encoding="utf-8"
    )
    print(text)
    if not report["unit_tests"].get("failures") and not report[
        "unit_tests"
    ].get("errors"):
        return
    print("--- unittest output ---")
    print(report["unit_tests"].get("output", ""))


if __name__ == "__main__":
    raise SystemExit(main())
