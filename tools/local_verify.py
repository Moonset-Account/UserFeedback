#!/usr/bin/env python3
"""
Local verification entry point for the ``resolve_fix_versions`` state-model
tests (cases C01-C04).

This wrapper invokes the real case implementations and their real assertions
directly; it does not merely print a success message. Every run emits one JSON
record per case with the actual assertion tally and replayable evidence
(synthetic catalogs, seeds, expected versus observed summaries).

Usage::

    python tools/local_verify.py --list
    python tools/local_verify.py --case C01
    python tools/local_verify.py --case C01,C02
    python tools/local_verify.py --case all

Exit status is non-zero if any assertion fails, an entry point is missing, a
case is unimplemented, no assertions were executed, or an entire case group is
skipped. The script never performs network access or package installation.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = Path(__file__).resolve().parent.parent / "test" / "_fix_state_cases.py"
MODEL_PATH = Path(__file__).resolve().parent.parent / "test" / "_fix_state_model.py"


def _load_module(module_name: str, path: Path) -> Any:
    """
    Load a helper module from an explicit path without touching tool config.
    """

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {module_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_cases_module() -> Any:
    """
    Load the case module plus its state-model dependency by explicit path.

    The workspace root is placed first on ``sys.path`` so that
    ``pip_audit`` resolves to this workspace's source tree and the test
    package import inside the helpers resolves locally.
    """

    root = str(WORKSPACE_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    _load_module("test._fix_state_model", MODEL_PATH)
    return _load_module("test._fix_state_cases", CASES_PATH)


def _summarize_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """
    Trim large generated evidence into an informative JSON-safe summary.
    """

    if "case" not in evidence:
        return evidence
    summary: dict[str, Any] = {"case": evidence["case"]}
    if "package" in evidence:
        summary["package"] = evidence["package"]
        summary["start"] = evidence.get("start")
        summary["catalog_rows"] = evidence.get("catalog_rows")
        summary["expected_outcome"] = evidence.get("expected_trace", {}).get("outcome")
        summary["observed_outcome"] = evidence.get("observed_outcome")
        summary["observed_queries"] = evidence.get("observed_queries")
        return summary
    if evidence["case"] == "C02":
        summary["sub_cases"] = sorted(evidence["evidence"])
        summary["dead_end"] = evidence["evidence"]["dead_end"]["observed_outcome"]
        return summary
    if evidence["case"] == "C03":
        summary["dependencies"] = sorted(evidence["per_dependency"])
        summary["all_service_calls"] = evidence["all_service_calls"]
        return summary
    if evidence["case"] == "C04":
        summary["seed_ranges"] = evidence["seed_ranges"]
        summary["sample_count"] = len(evidence["samples"])
        summary["first_sample"] = evidence["samples"][0]
        summary["first_reduced"] = evidence["reduced_samples"][0]
        summary["shortest_divergence_chain"] = evidence["shortest_divergence_chain"]
        return summary
    return summary


def _run_case(cases_module: Any, case_id: str) -> tuple[dict[str, Any], bool]:
    """
    Execute one case and build its JSON record.

    Returns the record and a boolean success flag. Any unexpected exception is
    captured into the record rather than printed as a bare traceback.
    """

    runner = getattr(cases_module.CASE_RUNNERS, "get", lambda _: None)(case_id)
    if runner is None or not callable(runner):
        return (
            {
                "case": case_id,
                "status": "missing_entry_point",
                "assertions": {"passed": 0, "failed": 0, "skipped": 0, "total": 0},
                "error": f"no runner registered for case {case_id}",
            },
            False,
        )

    model_module = sys.modules["test._fix_state_model"]
    check_recorder = model_module.CheckRecorder(raise_on_fail=False)
    try:
        evidence = runner(check_recorder)
    except BaseException as exc:  # noqa: BLE001 - verifier must report all failures
        return (
            {
                "case": case_id,
                "status": "unexpected_exception",
                "assertions": {
                    "passed": check_recorder.passed,
                    "failed": check_recorder.failed,
                    "skipped": 0,
                    "total": len(check_recorder.checks),
                },
                "error": f"{type(exc).__name__}: {exc}",
                "failures": check_recorder.failures(),
            },
            False,
        )

    total = len(check_recorder.checks)
    failed = check_recorder.failed
    if total == 0:
        status = "zero_assertions"
        success = False
    elif failed:
        status = "failed"
        success = False
    else:
        status = "passed"
        success = True

    return (
        {
            "case": case_id,
            "status": status,
            "assertions": {
                "passed": check_recorder.passed,
                "failed": failed,
                "skipped": 0,
                "total": total,
            },
            "failures": check_recorder.failures(),
            "evidence": _summarize_evidence(evidence),
        },
        success,
    )


def main(argv: list[str] | None = None) -> int:
    """
    Parse arguments, run the requested cases and emit JSON records.
    """

    parser = argparse.ArgumentParser(
        description="Local verifier for the resolve_fix_versions state-model cases"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list the available case identifiers and descriptions",
    )
    parser.add_argument(
        "--case",
        type=str,
        default=None,
        help="case identifier(s) to run: C01, C02, C03, C04, comma-separated, or 'all'",
    )
    args = parser.parse_args(argv)

    try:
        cases_module = _load_cases_module()
    except BaseException as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "status": "entry_point_missing",
                    "error": f"failed to load case module: {type(exc).__name__}: {exc}",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    if args.list:
        listing = [
            {"case": case_id, "description": cases_module.CASE_DESCRIPTIONS[case_id]}
            for case_id in sorted(cases_module.CASE_RUNNERS)
        ]
        print(json.dumps({"available_cases": listing}, indent=2, sort_keys=True))
        return 0

    if args.case is None:
        parser.error("one of --list or --case is required")

    requested = [part.strip().upper() for part in args.case.split(",") if part.strip()]
    if not requested:
        parser.error("--case requires at least one case identifier")
    if "ALL" in requested:
        requested = sorted(cases_module.CASE_RUNNERS)

    unknown = [case_id for case_id in requested if case_id not in cases_module.CASE_RUNNERS]
    if unknown:
        print(
            json.dumps(
                {"status": "unknown_case", "unknown": unknown},
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    records: list[dict[str, Any]] = []
    overall_success = True
    for case_id in requested:
        record, success = _run_case(cases_module, case_id)
        records.append(record)
        overall_success = overall_success and success

    print(json.dumps({"results": records}, indent=2, sort_keys=True))
    return 0 if overall_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
