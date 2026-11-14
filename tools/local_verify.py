#!/usr/bin/env python3
"""Local verification entry point for SEC-20261114-028.

Run with: /usr/local/bin/python3 tools/local_verify.py

This wrapper does three things and fails loudly if any of them is missing:

1. Discovers the unittest suite with ``unittest.defaultTestLoader.discover``,
   asserts ``countTestCases() >= 1`` and then actually runs it.  A zero-test
   run, an all-skipped run, an erroring run or any failure/errors make the
   wrapper exit non-zero (unittest's "0 tests => exit 0" is never trusted).
2. Runs the delivered sample/boundary assertions for this task case by case,
   printing each case's input, actual output and expected value, and failing
   if any differs.
3. Prints a summary and returns 0 only when both parts are fully green.
"""

import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

RESULTS = []  # list of dicts: case_id, description, input, expected, actual, ok


def add_result(case_id, description, input_desc, expected, actual, ok):
    RESULTS.append({
        "case_id": case_id,
        "description": description,
        "input": input_desc,
        "expected": expected,
        "actual": actual,
        "ok": bool(ok),
    })


def run_discovered_suite(test_dir, top_level_dir, stream=None):
    """Discover and run the suite, returning ``(ok, evidence)``.

    ``ok`` is false for a zero-test discovery, an all-skipped run, any failure
    or error, and any discovery/run count mismatch.  unittest's own
    "0 tests => exit 0" behaviour is never treated as success here.
    """
    stream = stream or sys.stdout
    suite = unittest.defaultTestLoader.discover(
        test_dir, top_level_dir=top_level_dir
    )
    count = suite.countTestCases()
    print(f"discovered test cases: {count}", file=stream)
    if count < 1:
        print("FAIL: discovered zero test cases", file=stream)
        return False, "discovered=0"
    result = unittest.TextTestRunner(verbosity=2, stream=stream).run(suite)
    run_count = result.testsRun
    skipped = len(result.skipped)
    executed = run_count - skipped
    print(
        f"executed (testsRun={run_count}, skipped={skipped}, "
        f"actually run={executed}, failures={len(result.failures)}, "
        f"errors={len(result.errors)}",
        file=stream,
    )
    ok = (executed >= 1
          and not result.failures
          and not result.errors
          and not result.unexpectedSuccesses
          and run_count == count)
    evidence = (f"discovered={count} testsRun={run_count} skipped={skipped} "
                f"executed={executed} failures={len(result.failures)} "
                f"errors={len(result.errors)}")
    return ok, evidence


def run_unit_suite():
    print("== Unit test suite ==")
    ok, evidence = run_discovered_suite(
        os.path.join(ROOT, "tests"), ROOT
    )
    add_result(
        "unit_suite",
        "unittest discovery actually runs >=1 executed test with no failures",
        f"unittest.defaultTestLoader.discover({os.path.join(ROOT, 'tests')!r})",
        "discovered >=1 and executed >=1, 0 failures, 0 errors, 0 skipped-only",
        evidence,
        ok,
    )
    if not ok:
        print("FAIL: unit suite did not fully pass", file=sys.stderr)
    return ok


def import_subject():
    from parser_regression import compare_reports, load_report, render_json
    return compare_reports, load_report, render_json


def fixture_case(summary, section, expected_samples, case_id, description):
    """Assert the emitted sample set of one section matches expectation."""
    rows = summary["sections"][section]
    actual = sorted(row["sample"] for row in rows)
    expected = sorted(expected_samples)
    ok = actual == expected
    add_result(
        case_id,
        description,
        f"fixtures/baseline.json + fixtures/current.json, section={section!r}",
        {"samples": expected},
        {"samples": actual},
        ok,
    )


def run_sample_checks():
    """Run the delivered task cases and record every actual value."""
    print("\n== Delivered sample / boundary checks ==")
    compare_reports, load_report, render_json = import_subject()

    baseline = load_report(os.path.join(ROOT, "fixtures", "baseline.json"))
    current = load_report(os.path.join(ROOT, "fixtures", "current.json"))
    summary = compare_reports(baseline, current)
    counts = summary["counts"]

    # C1: new failures identified.
    fixture_case(
        summary, "new_failures",
        ["brand-new", "flaky-net", "latency-probe", "skip-to-fail"],
        "C1-new-failures",
        "newly failing samples in the current build",
    )

    # C2: fixed sample.
    fixture_case(
        summary, "fixed",
        ["catastrophic-regex"],
        "C2-fixed",
        "sample that failed in the baseline and now passes",
    )

    # C3: persistent failures.
    fixture_case(
        summary, "persistent_failures",
        ["attr-quote", "crlf-normalize", "mixed-timeout", "utf8-bom"],
        "C3-persistent-failures",
        "samples that failed in both builds",
    )

    # C4: unstable = single pass + single fail in one mode/env group.
    fixture_case(
        summary, "unstable",
        ["nested-comment"],
        "C4-unstable",
        "one pass and one fail in the same group is unstable",
    )
    unstable_row = summary["sections"]["unstable"][0]
    add_result(
        "C4b-unstable-attempts",
        "unstable row keeps both attempt statuses",
        "fixtures/current.json nested-comment attempts 1..2",
        [{"attempt": 1, "status": "passed"}, {"attempt": 2, "status": "failed"}],
        [{"attempt": d["attempt"], "status": d["status"]}
         for d in unstable_row["attempt_details"]],
        [{"attempt": d["attempt"], "status": d["status"]}
         for d in unstable_row["attempt_details"]]
        == [{"attempt": 1, "status": "passed"}, {"attempt": 2, "status": "failed"}],
    )

    # C5: not executed vs stable pass.
    fixture_case(
        summary, "not_executed",
        ["skip-me"],
        "C5-not-executed",
        "all-skipped sample counted separately from stable passes",
    )
    add_result(
        "C5b-stable-pass-count",
        "stable passes counted independently of skipped rows",
        "fixtures pair, counts.stable_passes",
        2,
        counts["stable_passes"],
        counts["stable_passes"] == 2,
    )

    # C6: rename with unchanged content matches the original baseline entry.
    all_sample_cells = set()
    for section, rows in summary["sections"].items():
        for row in rows:
            sample = row["sample"]
            all_sample_cells.add(sample if isinstance(sample, str) else str(sample))
    add_result(
        "C6-rename-matches-baseline",
        "unchanged content under a new sample name is not reported as new or missing",
        "baseline old-name-sample -> current new-name-sample, same content 'x<y'",
        "neither name emitted as new failure or missing baseline",
        {"new_name_emitted": "new-name-sample" in all_sample_cells,
         "old_name_emitted": "old-name-sample" in all_sample_cells,
         "counts": counts},
        ("new-name-sample" not in all_sample_cells
         and "old-name-sample" not in all_sample_cells
         and counts["stable_passes"] == 2),
    )

    # C7: duplicate attempt numbers -> pending confirmation, not classified.
    fixture_case(
        summary, "needs_confirmation",
        ["dup-sample"],
        "C7-duplicate-attempt-conflict",
        "duplicate attempt number reported for confirmation, not classified",
    )
    conflict_row = summary["sections"]["needs_confirmation"][0]
    add_result(
        "C7b-conflict-detail",
        "conflict row exposes attempt number and both entries",
        "fixtures/current.json dup-sample attempts=1 (passed+failed)",
        {"status": "pending_confirmation", "attempt": 1, "entries": 2, "current_outcome": None},
        {"status": conflict_row["status"],
         "attempt": conflict_row["conflicts"][0]["attempt"],
         "entries": len(conflict_row["conflicts"][0]["entries"]),
         "current_outcome": conflict_row["current_outcome"]},
        (conflict_row["status"] == "pending_confirmation"
         and conflict_row["conflicts"][0]["attempt"] == 1
         and len(conflict_row["conflicts"][0]["entries"]) == 2
         and conflict_row["current_outcome"] is None),
    )

    # C8: comparability — same content in a different mode/env is not compared.
    fixture_case(
        summary, "scope_mismatch",
        ["scope-test"],
        "C8-mode-environment-comparability",
        "content seen only under another parse mode/environment is not compared",
    )
    scope_row = summary["sections"]["scope_mismatch"][0]
    add_result(
        "C8b-scope-evidence",
        "scope mismatch keeps the baseline scope evidence",
        "baseline scope-test strict/linux failed -> current lenient/linux passed",
        {"current_mode": "lenient", "baseline_mode": "strict",
         "baseline_outcome": "failed", "new_failures": 4},
        {"current_mode": scope_row["parse_mode"],
         "baseline_mode": scope_row["scope_evidence"][0]["parse_mode"],
         "baseline_outcome": scope_row["scope_evidence"][0]["outcome"],
         "new_failures": counts["new_failures"]},
        (scope_row["parse_mode"] == "lenient"
         and scope_row["scope_evidence"][0]["parse_mode"] == "strict"
         and scope_row["scope_evidence"][0]["outcome"] == "failed"
         and counts["new_failures"] == 4),
    )

    # C9: timeout origin distinguishes pre-existing timeout from new error class.
    timeout_rows = {row["sample"]: row
                    for section in ("new_failures", "persistent_failures", "fixed")
                    for row in summary["sections"][section]}
    add_result(
        "C9-timeout-origin",
        "pre-existing timeout vs new timeout vs timeout resolved are distinguished",
        "utf8-bom (base+curing timeout:30s), flaky-net (new timeout:60s), "
        "catastrophic-regex (base parse:stack_overflow -> now passed)",
        {"utf8-bom": "pre_existing_timeout", "flaky-net": "new_timeout",
         "catastrophic-regex": "no_timeout"},
        {name: timeout_rows[name]["timeout"]
         for name in ("utf8-bom", "flaky-net", "catastrophic-regex")},
        all(timeout_rows[name]["timeout"] == expected
            for name, expected in {"utf8-bom": "pre_existing_timeout",
                                   "flaky-net": "new_timeout",
                                   "catastrophic-regex": "no_timeout"}.items()),
    )
    # C9b: new error category inside an otherwise pre-existing timeout group.
    mixed = next(r for r in summary["sections"]["persistent_failures"]
                 if r["sample"] == "mixed-timeout")
    add_result(
        "C9b-new-error-category-vs-existing-timeout",
        "a new non-timeout error class is separated from the existing timeout",
        "mixed-timeout: base timeout:30s; current timeout:30s + parse:out_of_memory",
        {"new_failure_categories": ["parse:out_of_memory"],
         "pre_existing_failure_categories": ["timeout:30s"],
         "timeout": "pre_existing_timeout"},
        {"new_failure_categories": mixed["new_failure_categories"],
         "pre_existing_failure_categories": mixed["pre_existing_failure_categories"],
         "timeout": mixed["timeout"]},
        (mixed["new_failure_categories"] == ["parse:out_of_memory"]
         and mixed["pre_existing_failure_categories"] == ["timeout:30s"]
         and mixed["timeout"] == "pre_existing_timeout"),
    )

    # C10: every row carries the required comparison columns.
    required = {"summary", "parse_mode", "environment", "build_id",
                "attempts", "failure_categories", "identity", "timeout"}
    rows = [(section, row) for section, items in summary["sections"].items()
            for row in items]
    missing = [(section, sorted(required - set(row)))
               for section, row in rows if required - set(row)]
    add_result(
        "C10-row-columns",
        "every summary row carries summary/mode/build/env/attempts/category",
        f"{len(rows)} emitted rows across {len(summary['sections'])} sections",
        [],
        missing,
        not missing,
    )

    # C11: CLI runs end-to-end on the local files and reports the counts.
    cli = subprocess.run(
        [sys.executable, os.path.join(ROOT, "app.py"),
         "--baseline", os.path.join(ROOT, "fixtures", "baseline.json"),
         "--current", os.path.join(ROOT, "fixtures", "current.json"),
         "--format", "json"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    try:
        payload = json.loads(cli.stdout) if cli.returncode == 0 else {}
    except json.JSONDecodeError:
        payload = {}
    add_result(
        "C11-cli-end-to-end",
        "app.py reads local exports and emits the JSON summary",
        "app.py --baseline fixtures/baseline.json --current fixtures/current.json --format json",
        {"returncode": 0,
         "counts": {"new_failures": 4, "fixed": 1, "persistent_failures": 4,
                    "unstable": 1, "not_executed": 1, "needs_confirmation": 1}},
        {"returncode": cli.returncode,
         "counts": payload.get("counts")},
        cli.returncode == 0 and payload.get("counts", {}).get("new_failures") == 4
        and payload.get("counts", {}).get("fixed") == 1
        and payload.get("counts", {}).get("persistent_failures") == 4
        and payload.get("counts", {}).get("unstable") == 1
        and payload.get("counts", {}).get("not_executed") == 1
        and payload.get("counts", {}).get("needs_confirmation") == 1,
    )
    if cli.stderr:
        print("  app.py stderr: " + cli.stderr.strip().splitlines()[-1])

    # C12: invalid input is rejected rather than silently summarised.
    bad_path = os.path.join(HERE, "_bad_export.json")
    with open(bad_path, "w", encoding="utf-8") as handle:
        json.dump({"build_id": "b1", "results": [
            {"sample_name": "s", "parse_mode": "strict", "environment": "linux",
             "attempt": 1, "status": "passed", "failure_category": "parse:oops"}
        ]}, handle)
    try:
        from parser_regression import LoadError, parse_report
        with open(bad_path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        try:
            parse_report(document, source="bad")
            rejected = False
            message = "accepted"
        except LoadError as exc:
            rejected = True
            message = str(exc)
    finally:
        os.unlink(bad_path)
    add_result(
        "C12-invalid-input-rejected",
        "passed result carrying a failure_category is rejected with a reason",
        "local synthetic export: status=passed + failure_category=parse:oops",
        {"rejected": True, "reason_mentions": "only failed results"},
        {"rejected": rejected, "message": message},
        rejected and "only failed results" in message,
    )


def print_results():
    print("\n== Case results ==")
    for item in RESULTS:
        print(f"[{item['case_id']}] {item['description']}")
        print(f"  input:    {item['input']}")
        print(f"  expected: {json.dumps(item['expected'], default=str, ensure_ascii=False)}")
        print(f"  actual:   {json.dumps(item['actual'], default=str, ensure_ascii=False)}")
        print(f"  status:   {'PASS' if item['ok'] else 'FAIL'}")
    failures = [item for item in RESULTS if not item["ok"]]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    return not failures


def main():
    unit_ok = run_unit_suite()
    try:
        run_sample_checks()
    except Exception as exc:  # a crashing check is a failure, never a skip
        import traceback
        traceback.print_exc()
        add_result("sample_checks", "delivered sample checks run without exceptions",
                   "tools/local_verify.py sample checks", "no exception",
                   f"{type(exc).__name__}: {exc}", False)
    checks_ok = print_results()
    if unit_ok and checks_ok:
        print("\nVERIFY: PASS")
        return 0
    print("\nVERIFY: FAIL", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
