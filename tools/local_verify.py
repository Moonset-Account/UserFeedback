#!/usr/bin/env python3
"""Local verification entry point for SEC-20261114-009.

This wrapper is the single command the acceptance/recording step runs. It:

1. Discovers the unit-test suite via ``unittest.defaultTestLoader.discover
   ("tests")`` and hard-requires ``countTestCases() >= 1`` before running it.
2. Runs the suite and requires that at least one test actually executed and was
   *not* skipped. Zero discovered tests, an all-skipped run, an exception from
   the runner, or any failure/error makes the wrapper exit non-zero.
3. Independently replays the delivered sample/boundary assertions through the
   real ``app.py`` CLI (subprocess), recording input, expected, and actual for
   every case instead of trusting the unit-test exit code.
4. Prints a JSON report to stdout and exits 0 only when every check passed.

Usage:  python3 tools/local_verify.py [--json-out PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIME_BUDGET_SECONDS = 80.0
CLI_TIMEOUT_SECONDS = 20.0

EXIT_OK = 0
EXIT_FAILED = 1


class RecordingResult(unittest.TextTestResult):
    """TextTestResult that counts tests which actually ran and were not skipped."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.executed_not_skipped = 0
        self.skipped_tests: list[str] = []
        self.errored = False

    def addSuccess(self, test) -> None:  # noqa: N802 - unittest API
        super().addSuccess(test)
        self.executed_not_skipped += 1

    def addFailure(self, test, err) -> None:  # noqa: N802 - unittest API
        super().addFailure(test, err)
        self.executed_not_skipped += 1
        self.errored = True

    def addError(self, test, err) -> None:  # noqa: N802 - unittest API
        super().addError(test, err)
        self.executed_not_skipped += 1
        self.errored = True

    def addSkip(self, test, reason) -> None:  # noqa: N802 - unittest API
        super().addSkip(test, reason)
        self.skipped_tests.append(f"{test}: {reason}")

    def addExpectedFailure(self, test, err) -> None:  # noqa: N802
        super().addExpectedFailure(test, err)
        self.executed_not_skipped += 1

    def addUnexpectedSuccess(self, test) -> None:  # noqa: N802
        super().addUnexpectedSuccess(test)
        self.errored = True


def run_unit_suite() -> dict:
    """Discover and run tests/; return a structured, evidence-preserving report."""

    report: dict = {
        "check": "unit_suite",
        "case_id": "unit-suite",
        "discovery_root": "tests",
        "passed": False,
    }

    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    os.chdir(ROOT)

    started = time.monotonic()
    try:
        suite = unittest.defaultTestLoader.discover("tests")
    except Exception as exc:  # discovery itself must not be silently swallowed
        report["error"] = f"discovery failed: {type(exc).__name__}: {exc}"
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        return report

    discovered = suite.countTestCases()
    report["discovered_test_count"] = discovered
    if discovered < 1:
        report["error"] = "discovery found zero tests; refusing to report success"
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        return report

    runner = unittest.TextTestRunner(
        stream=sys.stderr, verbosity=2, resultclass=RecordingResult
    )
    try:
        result = runner.run(suite)
    except Exception as exc:
        report["error"] = f"test runner raised: {type(exc).__name__}: {exc}"
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        return report

    report["duration_seconds"] = round(time.monotonic() - started, 3)
    report["tests_run"] = result.testsRun
    report["executed_not_skipped"] = result.executed_not_skipped
    report["skipped"] = len(result.skipped_tests)
    report["skipped_detail"] = result.skipped_tests
    report["failures"] = len(result.failures)
    report["errors"] = len(result.errors)
    report["failure_detail"] = [
        {"test": str(test), "trace": trace.splitlines()[-3:]}
        for test, trace in (result.failures + result.errors)
    ]

    problems = []
    if result.testsRun < 1:
        problems.append("runner executed zero tests")
    if result.executed_not_skipped < 1:
        problems.append("every test was skipped; no executed evidence")
    if result.failures or result.errors:
        problems.append("one or more tests failed/errored")
    if not result.wasSuccessful():
        problems.append("unittest reported an unsuccessful run")

    report["problems"] = problems
    report["passed"] = not problems
    return report


# -- sample / boundary assertions through the real CLI ----------------------


def _run_cli(args: list[str], stdin_text: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "app.py"), *args],
        cwd=ROOT,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_SECONDS,
        check=False,
    )


def _cli_case(case_id: str, requirement: str, args: list[str], expected: dict) -> dict:
    """Run one CLI invocation and compare the observed facts to expectations."""

    entry: dict = {
        "check": "cli_sample",
        "case_id": case_id,
        "requirement": requirement,
        "command": ["app.py", *args],
        "expected": expected,
    }
    try:
        completed = _run_cli(args)
    except Exception as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["passed"] = False
        return entry

    entry["exit_code"] = completed.returncode
    entry["expected_exit_code"] = expected["exit_code"]
    try:
        entry["actual"] = json.loads(completed.stdout) if completed.stdout.strip() else {}
    except json.JSONDecodeError:
        entry["actual"] = {"raw_stdout": completed.stdout[:500]}
        entry["stderr"] = completed.stderr[-500:]
        entry["passed"] = False
        entry["error"] = "stdout was not JSON"
        return entry

    actual = entry["actual"]
    mismatches: list[str] = []
    if completed.returncode != expected["exit_code"]:
        mismatches.append(
            f"exit_code: expected {expected['exit_code']}, got {completed.returncode}"
        )
    for key, wanted in expected.get("fields", {}).items():
        got = _dig(actual, key)
        if got != wanted:
            mismatches.append(f"{key}: expected {wanted!r}, got {got!r}")

    entry["mismatches"] = mismatches
    entry["passed"] = not mismatches
    return entry


def _dig(payload, dotted: str):
    """Fetch ``a.b.0.c`` style paths from decoded JSON."""

    current = payload
    for part in dotted.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def run_cli_samples() -> list[dict]:
    fixtures = os.path.join(ROOT, "fixtures")
    config = os.path.join(fixtures, "config.json")
    batch = os.path.join(fixtures, "batch.json")
    allow = os.path.join(fixtures, "request_allow.json")

    cases: list[dict] = []

    cases.append(
        _cli_case(
            "cli-allow",
            "a request with room in both layers is admitted",
            ["decide", "--config", config, "--request", allow],
            {
                "exit_code": 0,
                "fields": {"decision": "allow", "blocked_layers": [], "layers.0.used": 1},
            },
        )
    )

    cases.append(
        _cli_case(
            "cli-batch-window-dedup",
            "batch order shows denial, dedup replay, window release and TTL expiry",
            ["batch", "--config", config, "--requests", batch],
            {
                "exit_code": 2,
                "fields": {
                    "allowed": 3,
                    "denied": 3,
                    "errors": 0,
                    "results.2.decision": "deny",
                    "results.2.blocked_layers": ["account"],
                    "results.2.retry_at": 10.0,
                    "results.3.deduplicated": True,
                    "results.3.decided_at": 2.0,
                    "results.4.decision": "allow",
                    "results.5.decision": "deny",
                    "results.5.retry_at": 20.0,
                },
            },
        )
    )

    cases.append(
        _cli_case(
            "cli-demo-scenarios",
            "demo reports window boundary, retry, and last-unit race as passing",
            ["demo"],
            {
                "exit_code": 0,
                "fields": {
                    "passed": True,
                    "failed_scenarios": [],
                    "scenarios.demo-window-boundary.passed": True,
                    "scenarios.demo-retry-advice.passed": True,
                    "scenarios.demo-last-unit-race.passed": True,
                    "scenarios.demo-two-layer-gate.passed": True,
                    "scenarios.demo-dedup-replay.passed": True,
                    "scenarios.demo-tenant-isolation.passed": True,
                },
            },
        )
    )

    conflict_cfg, conflict_reqs = _write_temp(
        {
            "account": {"limit": 5, "window_seconds": 10},
            "source": {"limit": 5, "window_seconds": 10},
            "dedup_ttl_seconds": 10,
        },
        [
            {"tenant": "t", "account": "a", "source": "192.0.2.1", "request_id": "rid", "now": 0.0},
            {"tenant": "t", "account": "b", "source": "192.0.2.1", "request_id": "rid", "now": 1.0},
        ],
    )
    cases.append(
        _cli_case(
            "cli-request-id-conflict",
            "same request id with a different account reports a conflict",
            ["batch", "--config", conflict_cfg, "--requests", conflict_reqs],
            {
                "exit_code": 4,
                "fields": {
                    "errors": 1,
                    "results.1.error": "request_id_conflict",
                    "results.1.existing.account": "a",
                    "results.1.incoming.account": "b",
                },
            },
        )
    )

    clock_cfg, clock_reqs = _write_temp(
        {
            "account": {"limit": 5, "window_seconds": 10},
            "source": {"limit": 5, "window_seconds": 10},
            "dedup_ttl_seconds": 10,
        },
        [
            {"tenant": "t", "account": "a", "source": "192.0.2.1", "request_id": "r1", "now": 10.0},
            {"tenant": "t", "account": "a", "source": "192.0.2.1", "request_id": "r2", "now": 9.0},
        ],
    )
    cases.append(
        _cli_case(
            "cli-clock-regression",
            "a reversed clock reports clock_regression",
            ["batch", "--config", clock_cfg, "--requests", clock_reqs],
            {"exit_code": 5, "fields": {"errors": 1, "results.1.error": "clock_regression"}},
        )
    )

    deny_cfg, deny_reqs = _write_temp(
        {
            "account": {"limit": 1, "window_seconds": 10},
            "source": {"limit": 9, "window_seconds": 10},
            "dedup_ttl_seconds": 10,
        },
        [
            {"tenant": "t", "account": "a", "source": "192.0.2.1", "request_id": "r1", "now": 0.0},
            {"tenant": "t", "account": "a", "source": "192.0.2.1", "request_id": "r2", "now": 1.0},
        ],
    )
    cases.append(
        _cli_case(
            "cli-deny-retry-advice",
            "a denial on the account layer reports the exact retry instant",
            ["batch", "--config", deny_cfg, "--requests", deny_reqs],
            {
                "exit_code": 2,
                "fields": {
                    "results.1.decision": "deny",
                    "results.1.blocked_layers": ["account"],
                    "results.1.retry_at": 10.0,
                },
            },
        )
    )

    cases.append(
        _cli_case(
            "cli-invalid-input",
            "a missing request file is reported as an error with a non-zero exit",
            ["decide", "--config", config, "--request", os.path.join(fixtures, "nope.json")],
            {"exit_code": 1, "fields": {"error": "io_error"}},
        )
    )

    return cases


def _write_temp(config: dict, requests: list[dict]) -> tuple[str, str]:
    import tempfile

    directory = tempfile.mkdtemp(prefix="quota-verify-")
    cfg_path = os.path.join(directory, "config.json")
    req_path = os.path.join(directory, "requests.json")
    with open(cfg_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle)
    with open(req_path, "w", encoding="utf-8") as handle:
        json.dump(requests, handle)
    return cfg_path, req_path


# -- entry point ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", help="optional path to also write the JSON report")
    args = parser.parse_args(argv)

    overall_start = time.monotonic()
    checks: list[dict] = [run_unit_suite()]
    checks.extend(run_cli_samples())
    elapsed = time.monotonic() - overall_start

    failed = [check["case_id"] for check in checks if not check.get("passed")]
    report = {
        "verification": "SEC-20261114-009",
        "elapsed_seconds": round(elapsed, 3),
        "time_budget_seconds": TIME_BUDGET_SECONDS,
        "within_time_budget": elapsed <= TIME_BUDGET_SECONDS,
        "checks": checks,
        "failed_cases": failed,
        "passed": not failed and elapsed <= TIME_BUDGET_SECONDS,
    }
    if elapsed > TIME_BUDGET_SECONDS:
        report.setdefault("problems", []).append(
            f"verification exceeded {TIME_BUDGET_SECONDS}s budget ({elapsed:.1f}s)"
        )

    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write(text + "\n")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")

    return EXIT_OK if report["passed"] else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
