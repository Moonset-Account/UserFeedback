#!/usr/bin/env python3
"""Local verification wrapper.

Runs the real unittest suite and the delivered sample/boundary assertions, and
exits non-zero unless all of the following hold:

* test discovery found at least one test case,
* the discovered suite actually executed at least one non-skipped test,
* every executed test passed,
* every sample/boundary case produced its expected exit code and contents.

Run with:  /usr/local/bin/python3 tools/local_verify.py [--verbose]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
APP = WORKSPACE / "app.py"
SAMPLES = WORKSPACE / "samples"

if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from importer import core  # noqa: E402
from importer.limits import Limits  # noqa: E402


class VerificationError(Exception):
    pass


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(APP), *args],
        capture_output=True,
        text=True,
        cwd=WORKSPACE,
        timeout=30,
    )


def _run_suite() -> tuple[int, int, int]:
    """Discover and run tests; return (discovered, executed, skipped)."""

    suite = unittest.defaultTestLoader.discover(str(WORKSPACE / "tests"))
    discovered = suite.countTestCases()
    print(f"[tests] discovered test cases: {discovered}")
    if discovered < 1:
        raise VerificationError("discovery found zero test cases")

    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)

    executed = result.testsRun - len(result.skipped)
    skipped = len(result.skipped)
    print(
        f"[tests] executed={result.testsRun} skipped={skipped} "
        f"effective={executed} failures={len(result.failures)} "
        f"errors={len(result.errors)}"
    )
    if executed < 1:
        raise VerificationError("no test actually executed (all skipped or none ran)")
    if result.failures or result.errors or result.unexpectedSuccesses:
        raise VerificationError("the test suite reported failures or errors")
    if not result.wasSuccessful():
        raise VerificationError("the test suite was not successful")
    return discovered, executed, skipped


def _check(case_id: str, expected, actual, failures: list[str]) -> bool:
    ok = expected == actual
    print(f"[{case_id}] {'PASS' if ok else 'FAIL'}")
    print(f"    expected: {json.dumps(expected, ensure_ascii=False)}")
    print(f"    actual:   {json.dumps(actual, ensure_ascii=False)}")
    if not ok:
        failures.append(case_id)
    return ok


def _sample_cli_cases(failures: list[str]) -> None:
    # S1: legacy + modern records all decode, old names map to new fields.
    proc = _run_cli("import", "samples/legacy_compatible.jsonl", "--compact")
    report = json.loads(proc.stdout)
    _check("S1.legacy_compatible.exit", 0, proc.returncode, failures)
    _check("S1.legacy_compatible.summary", {"records_seen": 4, "accepted": 4, "rejected": 0},
           report["summary"], failures)
    first = report["records"][0]
    _check(
        "S1.legacy_text_label.mapping",
        {
            "declared_task_type": "text_label",
            "canonical_task_type": "text_classification",
            "data": {
                "record_id": "legacy-1001",
                "text": "Which sentiment does this review express?",
                "labels": ["positive", "negative", "neutral"],
            },
            "extensions": {
                "legacy_source": "pipeline-2019",
                "legacy_tags": ["sentiment", "short-text"],
            },
        },
        {
            "declared_task_type": first["declared_task_type"],
            "canonical_task_type": first["canonical_task_type"],
            "data": first["data"],
            "extensions": first["extensions"],
        },
        failures,
    )
    _check(
        "S1.legacy_document_scan.mapping",
        {
            "record_id": "legacy-1002",
            "title": "Legacy onboarding memo",
            "paragraphs": [
                "Accounts are created by an operator.",
                "Approvals are recorded in the ledger.",
            ],
        },
        report["records"][1]["data"],
        failures,
    )
    _check(
        "S1.modern_document.verbatim_extensions",
        {"reviewer": "synthetic", "score": 3},
        report["records"][3]["extensions"]["annotations"],
        failures,
    )

    # S2: unknown task types rejected with field location; neighbours continue.
    proc = _run_cli("import", "samples/unknown_task_type.jsonl", "--compact")
    report = json.loads(proc.stdout)
    _check("S2.unknown_task_type.exit", 1, proc.returncode, failures)
    _check("S2.unknown_task_type.summary", {"records_seen": 4, "accepted": 2, "rejected": 2},
           report["summary"], failures)
    _check("S2.unknown_task_type.accepted_ids", ["keep-3001", "keep-3004"],
           [r["record_id"] for r in report["records"]], failures)
    _check(
        "S2.unknown_task_type.failure_locations",
        [
            {"line": 2, "record_id": "unknown-3002", "classification": "unregistered_task_type",
             "field": "$.task_type"},
            {"line": 3, "record_id": "unknown-3003", "classification": "unregistered_task_type",
             "field": "$.task_type"},
        ],
        [
            {
                "line": f["line"],
                "record_id": f["record_id"],
                "classification": f["classification"],
                "field": f["field"],
            }
            for f in report["failures"]
        ],
        failures,
    )

    # S3: nested structure beyond max_object_depth, neighbours continue.
    proc = _run_cli("import", "samples/nested_depth_overflow.jsonl", "--compact")
    report = json.loads(proc.stdout)
    _check("S3.nested_depth.exit", 1, proc.returncode, failures)
    _check("S3.nested_depth.summary", {"records_seen": 3, "accepted": 2, "rejected": 1},
           report["summary"], failures)
    _check("S3.nested_depth.accepted_ids", ["keep-4001", "keep-4003"],
           [r["record_id"] for r in report["records"]], failures)
    failure = report["failures"][0]
    _check(
        "S3.nested_depth.failure",
        {
            "line": 2,
            "record_id": "deep-4002",
            "classification": "depth_limit_exceeded",
            "field": "$.extensions.annotations.l1.l2.l3.l4",
            "detail_contains_depth_7": True,
        },
        {
            "line": failure["line"],
            "record_id": failure["record_id"],
            "classification": failure["classification"],
            "field": failure["field"],
            "detail_contains_depth_7": "object depth 7" in failure["detail"],
        },
        failures,
    )

    # S4: JSON array framing plus configured max_list_items boundary.
    proc = _run_cli(
        "import", "samples/list_items_overflow.json",
        "--limits", "samples/limits_demo.json", "--compact",
    )
    report = json.loads(proc.stdout)
    _check("S4.list_items.exit", 1, proc.returncode, failures)
    _check("S4.list_items.limits", {"max_file_bytes": 65536, "max_record_bytes": 4096,
                                    "max_object_depth": 6, "max_list_items": 3},
           report["limits"], failures)
    _check("S4.list_items.summary", {"records_seen": 3, "accepted": 2, "rejected": 1},
           report["summary"], failures)
    failure = report["failures"][0]
    _check(
        "S4.list_items.failure",
        {"line": 3, "record_id": "list-5002",
         "classification": "list_item_limit_exceeded", "field": "$.labels",
         "mentions_4_items": True},
        {
            "line": failure["line"],
            "record_id": failure["record_id"],
            "classification": failure["classification"],
            "field": failure["field"],
            "mentions_4_items": "list has 4 items" in failure["detail"],
        },
        failures,
    )


def _boundary_cases(failures: list[str]) -> None:
    """In-process boundary assertions, measured against real byte counts."""

    def modern(**overrides) -> dict:
        record = {
            "version": "2",
            "task_type": "text_classification",
            "record_id": "b-1",
            "text": "t" * 40,
            "labels": ["a"],
        }
        record.update(overrides)
        return record

    def line(record: dict) -> bytes:
        return json.dumps(record, ensure_ascii=False).encode()

    probe = modern()
    size = len(line(probe))
    at_limit = core.import_bytes(line(probe) + b"\n", limits=Limits(max_record_bytes=size))
    over_limit = core.import_bytes(line(probe) + b"\n", limits=Limits(max_record_bytes=size - 1))
    _check(
        "B1.record_bytes.exact_limit_vs_one_over",
        {"measured": size, "exact": ("accepted", 1), "one_over": ("record_size_limit_exceeded", 0)},
        {
            "measured": at_limit["records"][0]["byte_length"],
            "exact": ("accepted", at_limit["summary"]["accepted"]),
            "one_over": (
                over_limit["failures"][0]["classification"],
                over_limit["summary"]["accepted"],
            ),
        },
        failures,
    )

    raw = line(probe) + b"\n"
    file_at = core.import_bytes(raw, limits=Limits(max_file_bytes=len(raw)))
    file_over = core.import_bytes(raw, limits=Limits(max_file_bytes=len(raw) - 1))
    _check(
        "B2.file_bytes.exact_limit_vs_one_over",
        {"exact": ("accepted", 0), "one_over": ("input_size_limit_exceeded", 2)},
        {
            "exact": ("accepted", core.exit_code(file_at)),
            "one_over": (file_over["failures"][0]["classification"], core.exit_code(file_over)),
        },
        failures,
    )

    def at_depth(depth: int) -> dict:
        node: dict = {"leaf": True}
        for _ in range(depth - 3):
            node = {"child": node}
        return modern(extensions={"annotations": node})

    depth_at = core.import_bytes(line(at_depth(4)) + b"\n", limits=Limits(max_object_depth=4))
    depth_over = core.import_bytes(line(at_depth(5)) + b"\n", limits=Limits(max_object_depth=4))
    _check(
        "B3.object_depth.exact_limit_vs_one_over",
        {"exact_accepted": 1,
         "one_over": ("depth_limit_exceeded", "$.extensions.annotations.child.child")},
        {
            "exact_accepted": depth_at["summary"]["accepted"],
            "one_over": (
                depth_over["failures"][0]["classification"],
                depth_over["failures"][0]["field"],
            ),
        },
        failures,
    )

    list_at = core.import_bytes(
        line(modern(labels=["a", "b", "c"])) + b"\n", limits=Limits(max_list_items=3)
    )
    list_over = core.import_bytes(
        line(modern(labels=["a", "b", "c", "d"])) + b"\n", limits=Limits(max_list_items=3)
    )
    _check(
        "B4.list_items.exact_limit_vs_one_over",
        {"exact_accepted": 1, "one_over": ("list_item_limit_exceeded", "$.labels")},
        {
            "exact_accepted": list_at["summary"]["accepted"],
            "one_over": (
                list_over["failures"][0]["classification"],
                list_over["failures"][0]["field"],
            ),
        },
        failures,
    )

    mixed = (
        line(modern(record_id="good-1")).decode()
        + "\n"
        + "{broken json}\n"
        + line(modern(record_id="good-2")).decode()
        + "\n"
    ).encode()
    mixed_report = core.import_bytes(mixed)
    _check(
        "B5.invalid_json.isolation",
        {"summary": {"records_seen": 3, "accepted": 2, "rejected": 1},
         "failure": ("invalid_json", "$", 2)},
        {
            "summary": mixed_report["summary"],
            "failure": (
                mixed_report["failures"][0]["classification"],
                mixed_report["failures"][0]["field"],
                mixed_report["failures"][0]["line"],
            ),
        },
        failures,
    )

    # A whitelist lookup is data: hostile task types never load anything.
    hostile = [modern(record_id=f"h-{i}", task_type=t)
               for i, t in enumerate(["os.system", "__import__", "eval"])]
    hostile_raw = ("\n".join(json.dumps(r) for r in hostile) + "\n").encode()
    hostile_report = core.import_bytes(hostile_raw)
    _check(
        "B6.task_type.not_a_loader",
        {"accepted": 0, "classifications": ["unregistered_task_type"] * 3},
        {
            "accepted": hostile_report["summary"]["accepted"],
            "classifications": [f["classification"] for f in hostile_report["failures"]],
        },
        failures,
    )

    # Unreadable input file is an input-level error, not a crash.
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "missing.jsonl"
        unreadable = core.import_file(missing)
    _check(
        "B7.unreadable_input",
        ("input_unreadable", 2),
        (unreadable["failures"][0]["classification"], core.exit_code(unreadable)),
        failures,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    started = time.monotonic()
    failures: list[str] = []
    try:
        discovered, executed, skipped = _run_suite()
        print(f"[tests] discovery+execution verified ({discovered} discovered, "
              f"{executed} effective, {skipped} skipped)")
        _sample_cli_cases(failures)
        _boundary_cases(failures)
    except VerificationError as exc:
        print(f"[verify] FAILED: {exc}")
        return 1
    except Exception as exc:  # unexpected, never report success
        print(f"[verify] FAILED with unexpected {type(exc).__name__}: {exc}")
        return 1

    elapsed = time.monotonic() - started
    print(f"[verify] elapsed: {elapsed:.2f}s")
    if failures:
        print(f"[verify] FAILED cases: {', '.join(failures)}")
        return 1
    print("[verify] PASS: unittest suite plus all sample/boundary cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
