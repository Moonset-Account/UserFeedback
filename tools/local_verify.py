#!/usr/local/bin/python3
"""Local verification wrapper (stdlib only).

Responsibilities:
  1. Discover the unittest suite via unittest.defaultTestLoader.discover("tests")
     and require countTestCases() >= 1.
  2. Actually execute every discovered test and require at least one
     non-skipped test to run; any failure/error/zero-test case exits non-zero.
  3. Run the delivered sample/boundary assertions one by one and retain the
     actual vs expected result for each stable case_id.

A machine-readable report is written to tools/verification_report.json.
Run:  /usr/local/bin/python3 tools/local_verify.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import traceback
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from remediation import Action, Clock, PlanStatus, RemediationEngine  # noqa: E402
from remediation.scenario import build_demo_scenario, run_scenario  # noqa: E402

UTC = timezone.utc


def _ts(hour, day=14):
    return datetime(2026, 11, day, hour, 0, 0, tzinfo=UTC)


def sample_and_boundary_checks() -> list[dict]:
    """Direct engine/scenario assertions with retained actual results."""
    records: list[dict] = []

    def check(case_id, description, input_desc, expected, actual_producer):
        try:
            actual = actual_producer()
            passed = actual == expected
            error = None
        except Exception as exc:  # noqa: BLE001 - record any failure as evidence
            actual = f"EXCEPTION: {type(exc).__name__}: {exc}"
            passed = False
            error = traceback.format_exc()
        records.append(
            {
                "case_id": case_id,
                "description": description,
                "input": input_desc,
                "expected": expected,
                "actual": actual,
                "passed": passed,
                "traceback": error,
            }
        )

    # SC-01 sample: non-prod report locks impact points inside daily budget
    engine = RemediationEngine(Clock(_ts(9)), {"team-a": 10})
    plan = engine.report("A1", "team-a", "staging", "high", 6, None)
    check(
        "SC-01",
        "non-production report locks points within team daily budget",
        "budget=10, request=6 points, staging",
        {"status": PlanStatus.PENDING_EXECUTION, "locked": 6, "remaining": 4},
        lambda: {
            "status": plan.status,
            "locked": plan.locked_points,
            "remaining": engine.ledger.remaining("team-a", "2026-11-14"),
        },
    )

    # SC-02 boundary: exact-fit request consumes the whole budget
    engine = RemediationEngine(Clock(_ts(9)), {"team-a": 10})
    plan = engine.report("A1", "team-a", "dev", "low", 10, None)
    check(
        "SC-02",
        "exact-fit request consumes 100% of the daily budget",
        "budget=10, request=10 points",
        {"status": PlanStatus.PENDING_EXECUTION, "locked": 10, "remaining": 0},
        lambda: {
            "status": plan.status,
            "locked": plan.locked_points,
            "remaining": engine.ledger.remaining("team-a", "2026-11-14"),
        },
    )

    # SC-03 boundary: one point over budget locks nothing
    engine = RemediationEngine(Clock(_ts(9)), {"team-a": 10})
    engine.report("A1", "team-a", "staging", "high", 6, None)
    plan = engine.report("A2", "team-a", "staging", "high", 5, None)
    check(
        "SC-03",
        "request exceeding remaining budget locks zero points",
        "budget=10, already locked=6, request=5",
        {"status": PlanStatus.BUDGET_EXCEEDED, "locked": 0, "remaining": 4},
        lambda: {
            "status": plan.status,
            "locked": plan.locked_points,
            "remaining": engine.ledger.remaining("team-a", "2026-11-14"),
        },
    )

    # SC-04 sample: production resource only enters review
    engine = RemediationEngine(Clock(_ts(9)), {"team-a": 10})
    plan = engine.report("A1", "team-a", "production", "critical", 99, None)
    check(
        "SC-04",
        "production resource enters manual review without locking quota",
        "production, request=99 points, budget=10",
        {"status": PlanStatus.PENDING_REVIEW, "locked": 0, "remaining": 10},
        lambda: {
            "status": plan.status,
            "locked": plan.locked_points,
            "remaining": engine.ledger.remaining("team-a", "2026-11-14"),
        },
    )

    # SC-05 boundary: exemption window end is inclusive
    engine = RemediationEngine(Clock(_ts(12)), {"team-a": 10})
    plan = engine.report("A1", "team-a", "dev", "low", 3, (_ts(8), _ts(12)))
    check(
        "SC-05",
        "report at exemption window end instant is still exempt",
        "window 08:00..12:00 inclusive, report at 12:00",
        {"status": PlanStatus.EXEMPTED, "locked": 0},
        lambda: {"status": plan.status, "locked": plan.locked_points},
    )

    # SC-06 sample: one exempt member blocks the whole atomic group
    engine = RemediationEngine(Clock(_ts(9)), {"team-a": 10})
    actions = [
        Action("r1", "team-a", "dev", 2, None),
        Action("r2", "team-a", "dev", 3, (_ts(8), _ts(12))),
    ]
    plan = engine.report("A1", "team-a", "dev", "high", 0, None, actions=actions)
    check(
        "SC-06",
        "one exempt action makes the whole atomic group lock nothing",
        "group [2 pts non-exempt, 3 pts exempt]",
        {"status": PlanStatus.EXEMPTED, "locked": 0, "remaining": 10},
        lambda: {
            "status": plan.status,
            "locked": plan.locked_points,
            "remaining": engine.ledger.remaining("team-a", "2026-11-14"),
        },
    )

    # SC-07 sample: same-day cancellation releases unused quota
    engine = RemediationEngine(Clock(_ts(9)), {"team-a": 10})
    engine.report("A1", "team-a", "staging", "high", 6, None)
    plan = engine.cancel("A1")
    check(
        "SC-07",
        "same-day cancellation releases the locked quota",
        "lock 6 then cancel on 2026-11-14",
        {"status": PlanStatus.CANCELLED, "released": 6, "remaining": 10},
        lambda: {
            "status": plan.status,
            "released": plan.released_points,
            "remaining": engine.ledger.remaining("team-a", "2026-11-14"),
        },
    )

    # SC-08 boundary: budget resets on the next UTC day
    engine = RemediationEngine(Clock(_ts(23)), {"team-a": 10})
    engine.report("A1", "team-a", "staging", "high", 10, None)
    engine.clock.advance(hours=2)
    plan = engine.report("A2", "team-a", "staging", "low", 7, None)
    check(
        "SC-08",
        "daily budget resets after the controllable clock crosses midnight UTC",
        "day1 locks 10/10, advance 2h to next day, request 7",
        {"day": "2026-11-15", "status": PlanStatus.PENDING_EXECUTION, "locked": 7, "remaining": 3},
        lambda: {
            "day": plan.day,
            "status": plan.status,
            "locked": plan.locked_points,
            "remaining": engine.ledger.remaining("team-a", "2026-11-15"),
        },
    )

    # SC-09 sample: duplicate report reuses the original plan, no double lock
    engine = RemediationEngine(Clock(_ts(9)), {"team-a": 10})
    first = engine.report("A1", "team-a", "staging", "high", 6, None)
    second = engine.report("A1", "team-a", "staging", "high", 6, None)
    check(
        "SC-09",
        "duplicate anomaly report reuses the original plan",
        "same anomaly_id reported twice with 6 points each",
        {"same_plan": True, "reused": True, "locked": 6, "remaining": 4},
        lambda: {
            "same_plan": first is second,
            "reused": second.reused,
            "locked": second.locked_points,
            "remaining": engine.ledger.remaining("team-a", "2026-11-14"),
        },
    )

    # SC-10 boundary: zero impact points still yield a pending plan
    engine = RemediationEngine(Clock(_ts(9)), {"team-a": 10})
    plan = engine.report("A1", "team-a", "dev", "info", 0, None)
    check(
        "SC-10",
        "zero-impact remediation becomes a pending plan locking zero points",
        "request=0 points",
        {"status": PlanStatus.PENDING_EXECUTION, "locked": 0, "remaining": 10},
        lambda: {
            "status": plan.status,
            "locked": plan.locked_points,
            "remaining": engine.ledger.remaining("team-a", "2026-11-14"),
        },
    )

    # SC-11 sample: built-in cross-day demo end-to-end outcomes
    result = run_scenario(build_demo_scenario())
    plans = {p["anomaly_id"]: p for p in result["final_plans"]}
    check(
        "SC-11",
        "built-in demo reaches the expected final state for all 7 anomalies",
        "11-event built-in demo scenario",
        {
            "A1": "cancelled",
            "A2": "budget_exceeded",
            "A3": "pending_review",
            "A4": "exempted",
            "A5": "pending_execution",
            "A6": "cancelled",
            "A7": "pending_execution",
        },
        lambda: {aid: p["status"] for aid, p in sorted(plans.items())},
    )

    # SC-12 boundary: negative impact points are rejected
    def negative_raises():
        engine = RemediationEngine(Clock(_ts(9)), {"team-a": 10})
        try:
            engine.report("A1", "team-a", "dev", "low", -1, None)
        except ValueError:
            return "ValueError"
        return "no-exception"

    check(
        "SC-12",
        "negative impact points are rejected as invalid input",
        "impact_points=-1",
        "ValueError",
        negative_raises,
    )

    return records


def run_unittest_suite() -> dict:
    loader = unittest.defaultTestLoader
    suite = loader.discover("tests")
    discovered = suite.countTestCases()

    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2)
    result = runner.run(suite)

    skipped = len(result.skipped)
    executed_non_skipped = result.testsRun - skipped
    return {
        "discovered_cases": discovered,
        "tests_run": result.testsRun,
        "skipped": skipped,
        "executed_non_skipped": executed_non_skipped,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "successful": result.wasSuccessful(),
        "runner_output": stream.getvalue(),
        "failure_details": [
            {"test": str(t), "detail": detail} for t, detail in result.failures + result.errors
        ],
        "skipped_details": [{"test": str(t), "reason": reason} for t, reason in result.skipped],
    }


def main() -> int:
    report: dict = {"generated_at_utc": datetime.now(tz=UTC).isoformat()}

    try:
        unit = run_unittest_suite()
    except Exception:  # noqa: BLE001
        report["fatal"] = "unittest discovery/execution raised an exception"
        report["traceback"] = traceback.format_exc()
        _persist(report)
        print(report["traceback"])
        return 2

    report["unittest"] = unit
    print("=== unittest runner output ===")
    print(unit["runner_output"])

    try:
        checks = sample_and_boundary_checks()
    except Exception:  # noqa: BLE001
        report["fatal"] = "sample/boundary checks raised an exception"
        report["traceback"] = traceback.format_exc()
        _persist(report)
        print(report["traceback"])
        return 2

    report["sample_boundary_checks"] = checks
    passed = sum(1 for c in checks if c["passed"])

    print("=== sample / boundary assertions ===")
    for c in checks:
        flag = "PASS" if c["passed"] else "FAIL"
        print(f"[{flag}] {c['case_id']}: {c['description']}")
        print(f"        input:    {c['input']}")
        print(f"        expected: {c['expected']}")
        print(f"        actual:   {c['actual']}")
    print(f"sample checks: {passed}/{len(checks)} passed")
    print(
        "unittest: "
        f"discovered={unit['discovered_cases']} run={unit['tests_run']} "
        f"skipped={unit['skipped']} executed_non_skipped={unit['executed_non_skipped']} "
        f"failures={unit['failures']} errors={unit['errors']}"
    )

    _persist(report)

    problems = []
    if unit["discovered_cases"] < 1:
        problems.append("zero test cases discovered")
    if unit["executed_non_skipped"] < 1:
        problems.append("no test case was actually executed (all skipped or empty)")
    if not unit["successful"]:
        problems.append("unittest reported failures or errors")
    if passed != len(checks):
        problems.append(f"{len(checks) - passed} sample/boundary assertion(s) failed")

    if problems:
        print("VERIFICATION FAILED: " + "; ".join(problems))
        return 1
    print("VERIFICATION OK")
    return 0


def _persist(report: dict) -> None:
    out = ROOT / "tools" / "verification_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"report written to {out.relative_to(ROOT)}")


if __name__ == "__main__":
    raise SystemExit(main())
