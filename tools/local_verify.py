#!/usr/local/bin/python3
"""Local verification entry point for SEC-20261114-042.

This wrapper is deliberately strict about evidence quality.  It exits non-zero
when:

* unittest discovery finds zero test cases (unittest itself would exit 0),
* every discovered case is skipped, so nothing actually ran,
* any test fails or errors,
* any delivered sample / boundary assertion in this file fails,
* the run exceeds its finite time budget.

It never fabricates a PASS, never prints digest bytes, and reports the actual
observed value for every sample alongside the expected one.
"""

from __future__ import annotations

import argparse
import io
import os
import signal
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import digest_compare
import trace_audit
from synthetic_cases import DEFAULT_LENGTH, boundary_cases, build_cases

EXIT_OK = 0
EXIT_TEST_FAILURE = 1
EXIT_INSUFFICIENT = 2
EXIT_TIMEOUT = 3

TIME_BUDGET_SECONDS = 75


class _Budget:
    """Finite-time guard so the command always terminates."""

    def __init__(self, seconds: int):
        self.seconds = seconds
        self.expired = False

    def _handler(self, signum, frame):  # noqa: ANN001 - signal signature
        self.expired = True
        print(
            f"\nVERIFY RESULT: FAIL reason=timeout budget_seconds={self.seconds}",
            flush=True,
        )
        os._exit(EXIT_TIMEOUT)

    def __enter__(self) -> "_Budget":
        if hasattr(signal, "SIGALRM"):
            self._previous = signal.signal(signal.SIGALRM, self._handler)
            signal.setitimer(signal.ITIMER_REAL, self.seconds)
        return self

    def __exit__(self, *exc_info) -> None:
        if hasattr(signal, "SIGALRM"):
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self._previous)


def discover_suite() -> unittest.TestSuite:
    # The delivered contract requires literal discover("tests"); run from the
    # project root so the relative path resolves regardless of caller cwd.
    os.chdir(ROOT)
    return unittest.defaultTestLoader.discover("tests")


def run_suite(suite: unittest.TestSuite) -> tuple[bool, int, list[str]]:
    """Run the suite and return (success, executed_count, skipped_names).

    ``executed`` excludes skips so an all-skipped suite cannot look like work.
    """
    buffer = io.StringIO()
    runner = unittest.TextTestRunner(stream=buffer, verbosity=2)
    result = runner.run(suite)
    print(buffer.getvalue(), end="")
    skipped_names = [f"{test.id()}" for test, _reason in result.skipped]
    executed = result.testsRun - len(skipped_names)
    return result.wasSuccessful(), executed, skipped_names


def _describe(case) -> str:
    return f"length={case.length} {case.public_label()}"


def sample_checks() -> list[tuple[str, str, str, bool]]:
    """Delivered sample/boundary assertions with real values preserved.

    Returns (sample_id, input_description, actual, passed) tuples.  Each entry
    is executed, not simulated; the actual value is whatever the code produced.
    """
    checks: list[tuple[str, str, str, bool]] = []

    groups = [("per-position", build_cases(DEFAULT_LENGTH)), ("boundary", boundary_cases())]
    for group_name, cases in groups:
        for case in cases:
            try:
                actual = digest_compare.compare_digests(
                    case.left, case.right, case.length
                )
            except Exception as exc:  # noqa: BLE001 - record the real failure
                checks.append(
                    (
                        f"{group_name}/{case.case_id}/boolean",
                        _describe(case),
                        f"raised:{type(exc).__name__}",
                        False,
                    )
                )
                continue
            checks.append(
                (
                    f"{group_name}/{case.case_id}/boolean",
                    _describe(case),
                    str(actual),
                    actual is case.expected_equal,
                )
            )

    for group_name, cases in groups:
        for case in cases:
            try:
                _, trace = digest_compare.compare_digests_with_trace(
                    case.left, case.right, case.length
                )
            except Exception as exc:  # noqa: BLE001 - record the real failure
                checks.append(
                    (
                        f"{group_name}/{case.case_id}/trace",
                        f"length={case.length}",
                        f"raised:{type(exc).__name__}",
                        False,
                    )
                )
                continue
            expected = tuple(
                (kind, pos) for kind, pos in trace_audit.expected_signature(case.length)
            )
            actual = "none" if trace.signature == expected else "differs-from-spec"
            checks.append(
                (
                    f"{group_name}/{case.case_id}/trace",
                    f"steps={trace.step_count} length={case.length}",
                    actual,
                    trace.signature == expected,
                )
            )

    checks.append(self_length_guard_check())
    checks.extend(length_guard_checks())
    return checks


def self_length_guard_check() -> tuple[str, str, str, bool]:
    base = bytes(DEFAULT_LENGTH)
    try:
        digest_compare.compare_digests(base, base, DEFAULT_LENGTH)
    except Exception as exc:  # noqa: BLE001 - record the real failure
        return ("guard/equal-length", f"length={DEFAULT_LENGTH}", f"raised:{type(exc).__name__}", False)
    return ("guard/equal-length", f"length={DEFAULT_LENGTH}", "no-error", True)


def length_guard_checks() -> list[tuple[str, str, str, bool]]:
    base = bytes(DEFAULT_LENGTH)
    samples = [
        ("left-too-short", base[:-1], base),
        ("right-too-short", base, base[:-1]),
        ("left-empty", b"", base),
        ("both-too-long", base + b"\x00", base + b"\x00"),
    ]
    out: list[tuple[str, str, str, bool]] = []
    for sample_id, left, right in samples:
        try:
            digest_compare.compare_digests(left, right, DEFAULT_LENGTH)
        except digest_compare.DigestLengthError as exc:
            actual = f"DigestLengthError lengths={dict(exc.actual_lengths)}"
            passed = exc.expected_length == DEFAULT_LENGTH
        except Exception as exc:  # noqa: BLE001 - record the real failure
            actual = f"wrong-exception:{type(exc).__name__}"
            passed = False
        else:
            actual = "no-error-returned-boolean"
            passed = False
        out.append((f"length-guard/{sample_id}", f"expected_length={DEFAULT_LENGTH}", actual, passed))
    return out


def report_samples() -> tuple[int, int]:
    checks = sample_checks()
    passed = 0
    print("SAMPLES (actual values preserved; no digest bytes printed)")
    for sample_id, description, actual, ok in checks:
        status = "PASS" if ok else "FAIL"
        print(f"  sample_id={sample_id} input={description} actual={actual} status={status}")
        passed += 1 if ok else 0
    print(f"SAMPLE SUMMARY total={len(checks)} passed={passed} failed={len(checks) - passed}")
    return len(checks), passed


def decide_outcome(
    success: bool,
    discovered: int,
    executed: int,
    sample_total: int,
    sample_passed: int,
) -> tuple[int, str]:
    """Pure decision: map observed evidence to (exit_code, reason)."""
    if discovered < 1:
        return EXIT_INSUFFICIENT, "zero_tests_discovered"
    if not success:
        return EXIT_TEST_FAILURE, "unittest_failures_or_errors"
    if executed < 1:
        return EXIT_INSUFFICIENT, "no_actual_tests_executed"
    if sample_total < 1 or sample_passed != sample_total:
        return EXIT_TEST_FAILURE, "sample_assertion_failure"
    return EXIT_OK, "PASS"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local verification for SEC-20261114-042")
    parser.add_argument(
        "--timeout",
        type=int,
        default=TIME_BUDGET_SECONDS,
        help=f"hard time budget in seconds (default {TIME_BUDGET_SECONDS})",
    )
    args = parser.parse_args(argv)

    print(f"VERIFY start cwd={os.getcwd()} python={sys.version.split()[0]}")

    with _Budget(args.timeout):
        suite = discover_suite()
        discovered = suite.countTestCases()
        print(f"DISCOVERY discovered={discovered}")
        if discovered < 1:
            code, reason = decide_outcome(False, discovered, 0, 0, 0)
            print(f"VERIFY RESULT: FAIL reason={reason}")
            return code

        success, executed, skipped_names = run_suite(suite)
        print(f"EXECUTION discovered={discovered} executed={executed} skipped={len(skipped_names)}")
        for name in skipped_names:
            print(f"  skipped={name}")

        sample_total, sample_passed = report_samples()

        code, reason = decide_outcome(
            success, discovered, executed, sample_total, sample_passed
        )
        if code == EXIT_OK:
            print(
                "VERIFY RESULT: PASS "
                f"tests_executed={executed} samples_passed={sample_passed}/{sample_total}"
            )
        else:
            print(f"VERIFY RESULT: FAIL reason={reason}")
        return code


if __name__ == "__main__":
    raise SystemExit(main())
