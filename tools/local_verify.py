#!/usr/bin/env python3
"""Local verification entry point for the handle-inheritance audit module.

Usage:
    python -I tools/local_verify.py --list
    python -I tools/local_verify.py --case C01
    python -I tools/local_verify.py --case all

The wrapper loads the synthetic sample for the requested case, runs the real
``handleaudit`` analyzer, and evaluates hard assertions against the report.
A case exits non-zero on any failed assertion, a skipped/zero-assertion case,
a missing entry point, or an analyzer exception.  Output is one JSON object
per case containing assertion counts and an expected/observed summary.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from dataclasses import dataclass

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SAMPLE_DIR = os.path.join(REPO_ROOT, "samples")

CASE_FILES = {
    "C01": "c01_reuse_and_generation.json",
    "C02": "c02_flag_flip_during_launch.json",
    "C03": "c03_modes_and_duplicate_rights.json",
    "C04": "c04_missing_snapshot_and_gaps.json",
}

CASE_TITLES = {
    "C01": "Handle value reuse after close and process generation reuse",
    "C02": "Inheritable flag flips between launch begin and commit",
    "C03": "Default inheritance, explicit list and duplicate granted rights",
    "C04": "Missing snapshot and gapped event stream preserve unknowns",
}


@dataclass
class AssertionResult:
    name: str
    passed: bool
    expected: object
    observed: object
    detail: str = ""


class CaseFailure(Exception):
    """Raised by check helpers so a case run stops on its first bad fact."""


def _assert(
    results: list[AssertionResult],
    name: str,
    condition: bool,
    expected: object,
    observed: object,
    detail: str = "",
) -> None:
    results.append(
        AssertionResult(
            name=name,
            passed=bool(condition),
            expected=expected,
            observed=observed,
            detail=detail,
        )
    )
    if not condition:
        raise CaseFailure(detail or f"assertion failed: {name}")


def _load_sample(case_id: str) -> dict:
    path = os.path.join(SAMPLE_DIR, CASE_FILES[case_id])
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _launch(report: dict, launch_id: str) -> dict:
    for launch in report["launches"]:
        if launch["launch_id"] == launch_id:
            return launch
    raise CaseFailure(f"launch {launch_id} missing from report")


def _index_handles(launch: dict) -> dict[str, dict]:
    return {item["child_handle"]: item for item in launch["inherited_handles"]}


# ---------------------------------------------------------------------------
# Case assertions
# ---------------------------------------------------------------------------


def _assert_c01(report: dict, dataset: dict, results: list[AssertionResult]) -> dict:
    launch = _launch(report, "L1A")
    handles = _index_handles(launch)

    _assert(
        results,
        "no false sequence gaps",
        report["sequence_gaps"] == [],
        [],
        report["sequence_gaps"],
    )
    _assert(
        results,
        "snapshot present and window complete",
        launch["snapshot"]["present"] is True
        and launch["completeness"] == "complete",
        {"present": True, "completeness": "complete"},
        {
            "present": launch["snapshot"]["present"],
            "completeness": launch["completeness"],
        },
    )
    h256 = handles.get("0x100")
    _assert(
        results,
        "reused value 0x100 resolves to second open-generation object",
        h256 is not None
        and h256["status"] == "confirmed"
        and h256["object_id"] == "obj-file-B"
        and h256["source_instance"].endswith("_o2"),
        "obj-file-B via open generation o2",
        None if h256 is None else {
            "object_id": h256.get("object_id"),
            "source_instance": h256.get("source_instance"),
            "status": h256.get("status"),
        },
    )
    h264 = handles.get("0x108")
    _assert(
        results,
        "token 0x108 confirmed with snapshot rights and flagged non-compliant",
        h264 is not None
        and h264["status"] == "confirmed"
        and h264["object_category"] == "token"
        and h264["granted_rights"] == ["READ", "WRITE"]
        and h264["policy"]["compliant"] is False
        and h264["policy"]["extra_rights"] == ["WRITE"],
        {"status": "confirmed", "category": "token",
         "rights": ["READ", "WRITE"], "compliant": False},
        None if h264 is None else {
            "status": h264.get("status"),
            "category": h264.get("object_category"),
            "rights": h264.get("granted_rights"),
            "compliant": h264.get("policy", {}).get("compliant"),
        },
    )
    _assert(
        results,
        "first non-compliant inheritance is the token in L1A",
        report["first_noncompliant_inheritance"] is not None
        and report["first_noncompliant_inheritance"]["launch_id"] == "L1A"
        and report["first_noncompliant_inheritance"]["child_handle"] == "0x108",
        {"launch_id": "L1A", "child_handle": "0x108"},
        report["first_noncompliant_inheritance"],
    )
    _assert(
        results,
        "reused process pid 200 generation 2 kept distinct from generation 1",
        launch["child"] == {"pid": 200, "generation": 2}
        and all(
            anomaly["kind"] != "ambiguous_handle" for anomaly in report["anomalies"]
        ),
        {"pid": 200, "generation": 2, "ambiguous_handle_anomalies": 0},
        {
            "child": launch["child"],
            "ambiguous_handle_anomalies": sum(
                1 for a in report["anomalies"] if a["kind"] == "ambiguous_handle"
            ),
        },
    )
    return {
        "confirmed": [
            {"handle": h["child_handle"], "object_id": h["object_id"]}
            for h in launch["inherited_handles"]
            if h["status"] == "confirmed"
        ],
        "first_violation": report["first_noncompliant_inheritance"],
    }


def _assert_c02(report: dict, dataset: dict, results: list[AssertionResult]) -> dict:
    launch = _launch(report, "L2A")
    handles = _index_handles(launch)

    _assert(
        results,
        "no global sequence gaps (snapshot seq 9 occupies the stream)",
        report["sequence_gaps"] == [],
        [],
        report["sequence_gaps"],
    )
    h320 = handles.get("0x140")
    _assert(
        results,
        "handle made inheritable after begin is snapshot-confirmed",
        h320 is not None
        and h320["status"] == "confirmed"
        and h320["object_id"] == "obj-file-F"
        and h320["evidence"] == "snapshot",
        {"status": "confirmed", "object_id": "obj-file-F",
         "evidence": "snapshot"},
        None if h320 is None else {
            "status": h320.get("status"),
            "object_id": h320.get("object_id"),
            "evidence": h320.get("evidence"),
        },
    )
    h336 = handles.get("0x150")
    _assert(
        results,
        "handle made non-inheritable before snapshot is not inherited",
        h336 is not None and h336["status"] == "not_inherited",
        "not_inherited",
        None if h336 is None else h336.get("status"),
    )
    _assert(
        results,
        "snapshot-complete report flags zero violations",
        launch["policy_violations"] == []
        and report["first_noncompliant_inheritance"] is None
        and launch["unverified_count"] == 0,
        {"violations": 0, "first_violation": None, "unverified": 0},
        {
            "violations": len(launch["policy_violations"]),
            "first_violation": report["first_noncompliant_inheritance"],
            "unverified": launch["unverified_count"],
        },
    )
    _assert(
        results,
        "snapshot enumeration is three confirmed handles plus one rejected",
        sorted(handles) == ["0x140", "0x148", "0x150", "0x158"]
        and launch["confirmed_count"] == 3
        and launch["not_inherited_count"] == 1,
        {"handles": ["0x140", "0x148", "0x150", "0x158"], "confirmed": 3,
         "not_inherited": 1},
        {"handles": sorted(handles), "confirmed": launch["confirmed_count"],
         "not_inherited": launch["not_inherited_count"]},
    )
    return {
        "confirmed": [
            {"handle": h["child_handle"], "object_id": h["object_id"]}
            for h in launch["inherited_handles"]
            if h["status"] == "confirmed"
        ],
        "not_inherited": [
            h["child_handle"]
            for h in launch["inherited_handles"]
            if h["status"] == "not_inherited"
        ],
    }


def _assert_c03(report: dict, dataset: dict, results: list[AssertionResult]) -> dict:
    l3a = _launch(report, "L3A")
    l3b = _launch(report, "L3B")
    a = _index_handles(l3a)
    b = _index_handles(l3b)

    _assert(
        results,
        "L3A default mode: inheritable key store confirmed with mask 0x3",
        a.get("0x190") is not None
        and a["0x190"]["status"] == "confirmed"
        and a["0x190"]["granted_access"] == "0x3"
        and a["0x190"]["policy"]["category_allowed"] is True
        and a["0x190"]["policy"]["extra_rights"] == ["WRITE"]
        and a["0x190"]["policy"]["compliant"] is False,
        {"mask": "0x3", "extra_rights": ["WRITE"], "compliant": False},
        None if a.get("0x190") is None else {
            "mask": a["0x190"].get("granted_access"),
            "extra_rights": a["0x190"].get("policy", {}).get("extra_rights"),
            "compliant": a["0x190"].get("policy", {}).get("compliant"),
        },
    )
    h512 = a.get("0x200")
    _assert(
        results,
        "duplicated handle keeps event-granted READ, not source READ|WRITE",
        h512 is not None
        and h512["status"] == "confirmed"
        and h512["granted_access"] == "0x1"
        and h512["granted_rights"] == ["READ"]
        and any(node.get("via") == "duplicate" for node in h512["source_chain"])
        and any(
            node.get("source_access") == "0x3"
            and node.get("granted_access") == "0x1"
            for node in h512["source_chain"]
            if node.get("via") == "duplicate"
        ),
        {"granted_access": "0x1", "chain_has_duplicate": True},
        None if h512 is None else {
            "granted_access": h512.get("granted_access"),
            "chain": h512.get("source_chain"),
        },
    )
    _assert(
        results,
        "L3A non-inheritable mutex is rejected by the snapshot",
        a.get("0x1A0") is not None
        and a["0x1A0"]["status"] == "not_inherited",
        "not_inherited",
        None if a.get("0x1A0") is None else a["0x1A0"].get("status"),
    )
    _assert(
        results,
        "L3B explicit list overrides non-inheritable flag",
        b.get("0x1B0") is not None
        and b["0x1B0"]["status"] == "confirmed"
        and b["0x1B0"]["object_category"] == "pipe"
        and l3b["inheritance_mode"] == "explicit_list"
        and l3b["explicit_handles"] == ["0x1B0"],
        {"status": "confirmed", "mode": "explicit_list",
         "explicit_handles": ["0x1B0"]},
        None if b.get("0x1B0") is None else {
            "status": b["0x1B0"].get("status"),
            "mode": l3b.get("inheritance_mode"),
            "explicit_handles": l3b.get("explicit_handles"),
        },
    )
    _assert(
        results,
        "first violation overall is the L3A key store at the earliest commit",
        report["first_noncompliant_inheritance"] is not None
        and report["first_noncompliant_inheritance"]["launch_id"] == "L3A"
        and report["first_noncompliant_inheritance"]["child_handle"] == "0x190",
        {"launch_id": "L3A", "child_handle": "0x190"},
        report["first_noncompliant_inheritance"],
    )
    return {
        "L3A_confirmed": [
            {"handle": h["child_handle"], "mask": h["granted_access"]}
            for h in l3a["inherited_handles"]
            if h["status"] == "confirmed"
        ],
        "L3B_confirmed": [
            {"handle": h["child_handle"], "mask": h["granted_access"]}
            for h in l3b["inherited_handles"]
            if h["status"] == "confirmed"
        ],
    }


def _assert_c04(report: dict, dataset: dict, results: list[AssertionResult]) -> dict:
    l4a = _launch(report, "L4A")
    l4b = _launch(report, "L4B")
    a = _index_handles(l4a)
    b = _index_handles(l4b)

    _assert(
        results,
        "L4A missing snapshot recorded, not fabricated",
        l4a["snapshot"]["present"] is False
        and l4a["completeness"] == "incomplete"
        and report["launches_missing_snapshot"]
        == [{"launch_id": "L4A", "claimed_snapshot_seq": 7}],
        {"present": False, "missing_record": {
            "launch_id": "L4A", "claimed_snapshot_seq": 7}},
        {
            "present": l4a["snapshot"]["present"],
            "completeness": l4a["completeness"],
            "missing_record": report["launches_missing_snapshot"],
        },
    )
    h600 = a.get("0x258")
    _assert(
        results,
        "L4A inheritable token without corroboration stays unverified",
        h600 is not None
        and h600["status"] == "unverified"
        and h600["evidence"] == "replay_prediction_no_snapshot",
        {"status": "unverified", "evidence": "replay_prediction_no_snapshot"},
        None if h600 is None else {
            "status": h600.get("status"), "evidence": h600.get("evidence")},
    )
    h768 = a.get("0x300")
    _assert(
        results,
        "L4A duplicate into child is confirmed and is the located violation",
        h768 is not None
        and h768["status"] == "confirmed"
        and h768["evidence"] == "duplicate_event"
        and h768["object_category"] == "vault"
        and h768["policy"]["compliant"] is False
        and l4a["first_noncompliant_inheritance"] is not None
        and l4a["first_noncompliant_inheritance"]["child_handle"] == "0x300",
        {"status": "confirmed", "evidence": "duplicate_event",
         "category": "vault", "compliant": False},
        None if h768 is None else {
            "status": h768.get("status"),
            "evidence": h768.get("evidence"),
            "category": h768.get("object_category"),
            "compliant": h768.get("policy", {}).get("compliant"),
        },
    )
    gap = l4b["gap_in_window"]
    _assert(
        results,
        "L4B flags the inside-window sequence gap (missing seq 14)",
        gap is not None and gap.get("missing_seqs") == [14]
        and l4b["completeness"] == "incomplete",
        {"missing_seqs": [14], "completeness": "incomplete"},
        {"gap": gap, "completeness": l4b["completeness"]},
    )
    h616 = b.get("0x268")
    h624 = b.get("0x270")
    _assert(
        results,
        "L4B snapshot file confirmed; registry across gap stays unverified",
        h616 is not None
        and h616["status"] == "confirmed"
        and h616["object_id"] == "obj-file-G"
        and h624 is not None
        and h624["status"] == "unverified"
        and h624["evidence"] == "replay_prediction_gapped_window",
        {"0x268": "confirmed", "0x270": "unverified"},
        {
            "0x268": None if h616 is None else h616.get("status"),
            "0x270": None if h624 is None else h624.get("status"),
        },
    )
    _assert(
        results,
        "unverified registry is never counted as a confirmed violation",
        l4b["policy_violations"] == []
        and report["first_noncompliant_inheritance"] is not None
        and report["first_noncompliant_inheritance"]["launch_id"] == "L4A",
        {"L4B_violations": 0, "overall_first": "L4A"},
        {
            "L4B_violations": len(l4b["policy_violations"]),
            "overall_first": None
            if report["first_noncompliant_inheritance"] is None
            else report["first_noncompliant_inheritance"]["launch_id"],
        },
    )
    return {
        "L4A": {
            "confirmed": [
                h["child_handle"]
                for h in l4a["inherited_handles"]
                if h["status"] == "confirmed"
            ],
            "unverified": [
                h["child_handle"]
                for h in l4a["inherited_handles"]
                if h["status"] == "unverified"
            ],
        },
        "L4B": {
            "confirmed": [
                h["child_handle"]
                for h in l4b["inherited_handles"]
                if h["status"] == "confirmed"
            ],
            "unverified": [
                h["child_handle"]
                for h in l4b["inherited_handles"]
                if h["status"] == "unverified"
            ],
        },
    }


ASSERTION_FUNCS = {
    "C01": _assert_c01,
    "C02": _assert_c02,
    "C03": _assert_c03,
    "C04": _assert_c04,
}


def _run_case(case_id: str) -> dict:
    from handleaudit import analyze_dataset  # real implementation entry point

    dataset = _load_sample(case_id)
    results: list[AssertionResult] = []
    case_results: list[AssertionResult] = []
    error = None
    report = None
    observed_summary = None
    try:
        report = analyze_dataset(dataset)
        # Determinism: a second replay must produce identical output.
        report_again = analyze_dataset(dataset)
        _assert(
            results,
            "deterministic replay across two runs",
            json.dumps(report, sort_keys=True)
            == json.dumps(report_again, sort_keys=True),
            "identical JSON reports",
            "identical JSON reports",
        )
        before = len(results)
        observed_summary = ASSERTION_FUNCS[case_id](report, dataset, results)
        case_results = results[before:]
    except CaseFailure:
        # The failing assertion is the last one appended; everything from the
        # first case-level assertion onward belongs to the case itself.
        case_results = results[1:] if len(results) > 1 else []
    except Exception as exc:  # analyzer/entry-point failure is a hard failure
        error = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc().splitlines()[-5:],
        }

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    case_assertion_count = len(case_results)
    # Zero case-level assertions or an exception means the case cannot pass.
    ok = (
        error is None
        and case_assertion_count > 0
        and passed > 0
        and failed == 0
    )
    return {
        "case": case_id,
        "title": CASE_TITLES[case_id],
        "sample": os.path.relpath(
            os.path.join(SAMPLE_DIR, CASE_FILES[case_id]), REPO_ROOT
        ),
        "seed": dataset.get("seed"),
        "ok": ok,
        "assertion_count": case_assertion_count,
        "total_checks_including_determinism": len(results),
        "passed": sum(1 for r in case_results if r.passed),
        "failed": sum(1 for r in case_results if not r.passed),
        "skipped": 0,
        "determinism_check": results[0].passed if results else False,
        "error": error,
        "assertions": [
            {
                "name": r.name,
                "passed": r.passed,
                "expected": r.expected,
                "observed": r.observed,
                "detail": r.detail,
            }
            for r in case_results
        ],
        "observed_summary": observed_summary,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local verification for the handle inheritance analyzer"
    )
    parser.add_argument("--list", action="store_true", help="list available cases")
    parser.add_argument(
        "--case",
        choices=sorted(CASE_FILES) + ["all"],
        help="case id to run, or 'all'",
    )
    args = parser.parse_args(argv)

    if args.list:
        print(
            json.dumps(
                [
                    {"case": cid, "title": CASE_TITLES[cid],
                     "sample": os.path.relpath(
                         os.path.join(SAMPLE_DIR, CASE_FILES[cid]), REPO_ROOT)}
                    for cid in sorted(CASE_FILES)
                ],
                indent=2,
            )
        )
        return 0

    if not args.case:
        parser.error("one of --list or --case is required")

    case_ids = sorted(CASE_FILES) if args.case == "all" else [args.case]
    outputs = [_run_case(case_id) for case_id in case_ids]
    print(json.dumps(outputs if len(outputs) > 1 else outputs[0], indent=2))
    return 0 if all(item["ok"] for item in outputs) else 1


if __name__ == "__main__":
    sys.exit(main())
