#!/usr/bin/env python3
"""Local replay/verification entry point for DSSE threshold evidence.

This harness runs the *same* scenario implementations as the unittest suite
(see ``tests/dsse_threshold_support.py``) but with an independent assertion
recorder, so every executed assertion is counted and an explicit JSON
evidence record is emitted.  It deliberately does not re-implement any
verification logic: it only constructs inputs and calls the public
``securesystemslib.dsse.Envelope.verify`` API.

Usage::

    python -I tools/local_verify.py --list
    python -I tools/local_verify.py --case C01
    python -I tools/local_verify.py --case C02 C03 C04

Exit status:
    0  every selected case passed and each carried at least one assertion
    1  one or more assertions failed, a case was missing/skipped, a case
       carried zero assertions, or an unexpected exception escaped a
       scenario
    2  invalid command line arguments
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Make the repository root importable when run with "python -I" (which does
# not add the current working directory to sys.path automatically).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.dsse_threshold_support import SCENARIOS, Checker  # noqa: E402


class AssertionFailure(Exception):
    """Raised by the recorder when a single assertion does not hold."""


@dataclass
class RaisesContext(AbstractContextManager[Any]):
    """Context manager backing :meth:`Recorder.assert_raises`."""

    recorder: "Recorder"
    expected: type[BaseException]
    label: str
    exception: BaseException | None = None

    def __enter__(self) -> "RaisesContext":
        return self

    def __exit__(self, exc_type, exc_value, exc_tb) -> bool:
        if exc_type is None:
            self.recorder._record_failure(
                f"{self.label}: expected {self.expected.__name__} but nothing "
                "was raised"
            )
            return False
        if not issubclass(exc_type, self.expected):
            self.recorder._record_failure(
                f"{self.label}: expected {self.expected.__name__}, got "
                f"{exc_type.__name__}: {exc_value}"
            )
            return False
        self.exception = exc_value
        # A matched exception is one successful assertion.
        self.recorder.passed += 1
        # Swallow the expected exception, mirroring unittest.assertRaises.
        return True


class Recorder(Checker):
    """Assertion adapter that counts every check and captures failures."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.failures: list[str] = []

    @property
    def total(self) -> int:
        return self.passed + self.failed

    def _record_failure(self, message: str) -> None:
        self.failed += 1
        self.failures.append(message)
        raise AssertionFailure(message)

    def assert_equal(self, actual: Any, expected: Any, msg: str) -> None:
        if actual == expected:
            self.passed += 1
        else:
            self._record_failure(
                f"{msg} | expected={expected!r}, observed={actual!r}"
            )

    def assert_true(self, value: bool, msg: str) -> None:
        if bool(value):
            self.passed += 1
        else:
            self._record_failure(f"{msg} | observed={value!r} is not truthy")

    def assert_in(self, member: Any, container: Any, msg: str) -> None:
        try:
            contained = member in container
        except TypeError as exc:  # pragma: no cover - defensive guard
            self._record_failure(f"{msg} | membership check failed: {exc}")
            return
        if contained:
            self.passed += 1
        else:
            self._record_failure(
                f"{msg} | {member!r} not in {container!r}"
            )

    def assert_raises(
        self, exception: type[BaseException], msg: str
    ) -> RaisesContext:
        # Entering the context is not itself an assertion; the outcome is
        # recorded when the context block exits.
        return RaisesContext(self, exception, msg)


def _json_default(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, set):
        return sorted(value)
    return repr(value)


def _run_case(case_id: str) -> dict[str, Any]:
    scenarios = SCENARIOS.get(case_id, [])
    record: dict[str, Any] = {
        "case": case_id,
        "assertions": {
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "total": 0,
        },
        "status": "unknown",
        "scenarios": [],
    }

    for scenario in scenarios:
        recorder = Recorder()
        scenario_record: dict[str, Any] = {
            "name": scenario.name,
            "description": scenario.description,
            "assertions": recorder,
        }
        result = None
        try:
            result = scenario.func(recorder)
        except AssertionFailure:
            # A failed assertion was already counted by the recorder.
            pass
        except Exception as exc:
            # An unexpected escape means the scenario itself is broken; the
            # evidence harness must keep going and report it as a failure.
            recorder.failed += 1
            recorder.failures.append(
                f"unexpected {type(exc).__name__} escaped scenario "
                f"{scenario.name}: {exc}"
            )
            record["traceback"] = traceback.format_exc().splitlines()[-5:]

        scenario_record["assertions"] = {
            "passed": recorder.passed,
            "failed": recorder.failed,
            "skipped": 0,
            "total": recorder.total,
        }
        scenario_record["failures"] = recorder.failures
        if result is not None:
            scenario_record["seed"] = result.seed
            scenario_record["expected"] = result.expected
            scenario_record["observed"] = result.observed

        if recorder.failed or recorder.total == 0:
            scenario_record["status"] = "failed"
        else:
            scenario_record["status"] = "passed"
        record["scenarios"].append(scenario_record)

        for key in ("passed", "failed", "total"):
            record["assertions"][key] += scenario_record["assertions"][key]

    if not scenarios:
        record["status"] = "missing"
    elif record["assertions"]["failed"]:
        record["status"] = "failed"
    elif record["assertions"]["total"] == 0:
        # Zero-assertion groups cannot prove anything and fail the run.
        record["status"] = "failed"
        record["assertions"]["failed"] += 1
        record["scenarios"].append(
            {
                "name": "__group_guard__",
                "status": "failed",
                "failures": [
                    f"{case_id}: no assertions were executed; an empty or "
                    "skipped group is not evidence"
                ],
            }
        )
    else:
        record["status"] = "passed"

    return record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay DSSE Envelope.verify threshold evidence cases."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--list",
        action="store_true",
        help="List available cases and their scenarios and exit.",
    )
    group.add_argument(
        "--case",
        nargs="+",
        metavar="CASE",
        choices=sorted(SCENARIOS),
        help="Case(s) to run, e.g. --case C01 or --case C01 C04.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list:
        listing = {
            case_id: [
                {"name": scenario.name, "description": scenario.description}
                for scenario in scenarios
            ]
            for case_id, scenarios in sorted(SCENARIOS.items())
        }
        print(json.dumps({"cases": listing}, indent=2, default=_json_default))
        return 0

    records = [_run_case(case_id) for case_id in args.case]
    totals = {"passed": 0, "failed": 0, "skipped": 0, "total": 0}
    for record in records:
        for key in totals:
            totals[key] += record["assertions"][key]

    all_passed = all(record["status"] == "passed" for record in records)
    summary = {
        "tool": "tools/local_verify.py",
        "cases": records,
        "totals": totals,
        "status": "passed" if all_passed else "failed",
    }
    print(json.dumps(summary, indent=2, default=_json_default))
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
