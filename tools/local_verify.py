#!/usr/bin/env python3
"""Local verification wrapper for SEC-20261114-005.

Runs two kinds of checks and exits non-zero unless everything passes:

1. The unittest suite in tests/ -- discovered via
   unittest.defaultTestLoader.discover("tests"). Discovery must find at
   least one test, at least one test must actually execute (not be
   skipped), and there must be no failures or errors. A zero-test or
   all-skipped run is a failure, never a silent pass.

2. The delivered sample/boundary assertions: an end-to-end synthetic
   scenario exercising the two-step cleanup requirements. Each assertion
   has a stable case_id and its actual result is recorded next to the
   expected value in verification_results.json.

Run from anywhere:  /usr/local/bin/python3 tools/local_verify.py
"""

import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

RESULTS_FILE = os.path.join(ROOT, "verification_results.json")
NOW = datetime.date(2026, 11, 14)


# ---------------------------------------------------------------------------
# part 1: unittest suite
# ---------------------------------------------------------------------------

def run_unit_suite():
    suite = unittest.defaultTestLoader.discover("tests")
    total = suite.countTestCases()
    print(f"[verify] discovered {total} unittest case(s) in tests/")
    if total < 1:
        print("[verify] ERROR: zero tests discovered -- treating as failure")
        return None
    runner = unittest.TextTestRunner(verbosity=1, stream=sys.stdout)
    result = runner.run(suite)
    executed = result.testsRun - len(result.skipped)
    print(f"[verify] unittest testsRun={result.testsRun} "
          f"skipped={len(result.skipped)} executed={executed} "
          f"failures={len(result.failures)} errors={len(result.errors)}")
    if executed < 1:
        print("[verify] ERROR: no unittest actually executed "
              "(zero run or all skipped) -- treating as failure")
        return None
    return result


# ---------------------------------------------------------------------------
# part 2: delivered sample / boundary assertions
# ---------------------------------------------------------------------------

def _write_json(path, payload):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def _add_attachment(data_dir, index, aid, case_id, created, as_dir=False):
    rel = os.path.join("files", case_id, aid + ".bin")
    abspath = os.path.join(data_dir, rel)
    os.makedirs(os.path.dirname(abspath), exist_ok=True)
    if as_dir:
        os.makedirs(abspath, exist_ok=True)
    else:
        with open(abspath, "wb") as fh:
            fh.write(("content-v1 of " + aid + "\n").encode("utf-8"))
    index["attachments"].append({
        "attachment_id": aid, "case_id": case_id, "space": "support",
        "path": rel, "version": 1, "created_at": created,
    })
    return abspath


def run_sample_assertions():
    import cleanup_core as core

    records = []

    def record(case_id, assertion, expected, actual):
        records.append({
            "case_id": case_id,
            "assertion": assertion,
            "expected": expected,
            "actual": actual,
            "pass": expected == actual,
        })
        mark = "PASS" if expected == actual else "FAIL"
        print(f"[sample] {case_id}: {mark} "
              f"expected={expected!r} actual={actual!r} :: {assertion}")

    data_dir = tempfile.mkdtemp(prefix="sec005-verify-")
    try:
        index = {"attachments": []}
        paths = {}
        paths["att-old"] = _add_attachment(data_dir, index, "att-old", "case-1", "2025-01-01")
        paths["att-boundary"] = _add_attachment(data_dir, index, "att-boundary", "case-2", "2025-11-14")
        _add_attachment(data_dir, index, "att-fresh", "case-3", "2026-11-01")
        paths["att-case-hold"] = _add_attachment(data_dir, index, "att-case-hold", "case-4", "2025-01-01")
        paths["att-att-hold"] = _add_attachment(data_dir, index, "att-att-hold", "case-5", "2025-01-01")
        paths["att-inactive"] = _add_attachment(data_dir, index, "att-inactive", "case-6", "2025-01-01")
        paths["att-replaced"] = _add_attachment(data_dir, index, "att-replaced", "case-7", "2025-01-01")
        paths["att-new-hold"] = _add_attachment(data_dir, index, "att-new-hold", "case-8", "2025-01-01")
        paths["att-retry"] = _add_attachment(data_dir, index, "att-retry", "case-9", "2025-01-01", as_dir=True)

        _write_json(os.path.join(data_dir, "spaces.json"),
                    {"spaces": {"support": {"retention_days": 365}}})
        _write_json(os.path.join(data_dir, "holds.json"), {"holds": [
            {"hold_id": "H-CASE", "scope": "case", "target_id": "case-4", "active": True},
            {"hold_id": "H-OFF", "scope": "attachment", "target_id": "att-att-hold", "active": False},
            {"hold_id": "H-ATT", "scope": "attachment", "target_id": "att-att-hold", "active": True},
            {"hold_id": "H-INACTIVE", "scope": "case", "target_id": "case-6", "active": False},
        ]})
        _write_json(os.path.join(data_dir, "index.json"), index)

        # -- step 1: operations confirms the deletion list ------------------
        confirmed = core.plan_confirmation(data_dir, NOW)
        planned = {entry["attachment_id"] for entry in confirmed}
        record("CASE-RETENTION-FRESH",
               "within-retention attachment is not confirmed for deletion",
               False, "att-fresh" in planned)

        # -- changes between confirmation and execution ---------------------
        # att-replaced is replaced: content version bumped 1 -> 2
        for att in index["attachments"]:
            if att["attachment_id"] == "att-replaced":
                att["version"] = 2
        _write_json(os.path.join(data_dir, "index.json"), index)
        with open(paths["att-replaced"], "wb") as fh:
            fh.write(b"content-v2 of att-replaced\n")
        # att-new-hold is newly placed under a legal freeze
        holds = {"holds": json.load(open(os.path.join(data_dir, "holds.json")))["holds"]}
        holds["holds"].append({"hold_id": "H-NEW", "scope": "attachment",
                               "target_id": "att-new-hold", "active": True})
        _write_json(os.path.join(data_dir, "holds.json"), holds)

        # -- step 2: executor processes the confirmed list ------------------
        result1 = core.execute_confirmed(data_dir, NOW)
        out1 = {o["attachment_id"]: o for o in result1["outcomes"]}

        record("CASE-EXPIRED-DELETE",
               "expired attachment without holds is deleted",
               "deleted", out1["att-old"]["status"])
        record("CASE-EXPIRED-FILE-GONE",
               "deleted attachment file no longer exists on disk",
               False, os.path.exists(paths["att-old"]))
        record("CASE-RETENTION-BOUNDARY",
               "attachment exactly retention_days old is expired (age >= retention)",
               "deleted", out1["att-boundary"]["status"])
        record("CASE-HOLD-CASE",
               "active case-level hold blocks deletion with reason",
               "legal_hold:H-CASE", out1["att-case-hold"]["reason"])
        record("CASE-HOLD-ANY-ACTIVE",
               "any one of several holds being active blocks (inactive ignored)",
               "legal_hold:H-ATT", out1["att-att-hold"]["reason"])
        record("CASE-HOLD-INACTIVE",
               "inactive hold alone does not block deletion",
               "deleted", out1["att-inactive"]["status"])
        record("CASE-VERSION-REPLACED",
               "attachment replaced after confirmation is skipped",
               "version_changed", out1["att-replaced"]["reason"])
        with open(paths["att-replaced"], "rb") as fh:
            replaced_content = fh.read().decode("utf-8").strip()
        record("CASE-VERSION-PRESERVED",
               "new version content is preserved (confirmed list not applied)",
               "content-v2 of att-replaced", replaced_content)
        record("CASE-HOLD-NEW",
               "freeze added after confirmation blocks deletion",
               "legal_hold:H-NEW", out1["att-new-hold"]["reason"])
        record("CASE-RETRY-FIRST-RUN",
               "transient I/O error marks entry pending_retry",
               "pending_retry", out1["att-retry"]["status"])
        record("CASE-RESULT-COUNTS",
               "first run counts deleted/skipped/pending_retry",
               {"deleted": 3, "skipped": 4, "pending_retry": 1, "pending": 0},
               result1["counts"])
        record("CASE-INDEX-UPDATED",
               "deleted attachments are removed from the file index",
               False, any(a["attachment_id"] == "att-old"
                          for a in json.load(open(os.path.join(data_dir, "index.json")))["attachments"]))

        # -- rerun: resume remaining entries, keep terminal ones ------------
        shutil.rmtree(paths["att-retry"])  # clear the transient failure
        with open(paths["att-retry"], "wb") as fh:
            fh.write(b"content-v1 of att-retry\n")
        result2 = core.execute_confirmed(data_dir, NOW)
        out2 = {o["attachment_id"]: o for o in result2["outcomes"]}
        record("CASE-RETRY-RESUME",
               "pending_retry entry is retried and deleted on rerun",
               "deleted", out2["att-retry"]["status"])
        record("CASE-RESUME-JOURNAL",
               "previously deleted entry is reported from journal, not reprocessed",
               True, out2["att-old"]["from_journal"])
        record("CASE-SKIPPED-TERMINAL",
               "skipped entry stays terminal on rerun (new round needed)",
               True, out2["att-case-hold"]["from_journal"])
        record("CASE-RESUME-COUNTS",
               "rerun counts reflect final journal state",
               {"deleted": 4, "skipped": 4, "pending_retry": 0, "pending": 0},
               result2["counts"])
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)

    return records


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    test_result = run_unit_suite()

    records = []
    sample_error = None
    try:
        records = run_sample_assertions()
    except Exception as exc:  # noqa: BLE001 -- record, then fail below
        sample_error = f"{type(exc).__name__}: {exc}"
        print(f"[verify] ERROR during sample assertions: {sample_error}")

    summary = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "unit_tests": None if test_result is None else {
            "testsRun": test_result.testsRun,
            "skipped": len(test_result.skipped),
            "failures": len(test_result.failures),
            "errors": len(test_result.errors),
            "wasSuccessful": test_result.wasSuccessful(),
        },
        "sample_cases": records,
        "sample_error": sample_error,
    }
    with open(RESULTS_FILE, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
        fh.write("\n")

    tests_ok = test_result is not None and test_result.wasSuccessful()
    samples_ok = (sample_error is None and len(records) >= 1
                  and all(r["pass"] for r in records))
    ok = tests_ok and samples_ok
    print(f"[verify] unit_tests={'PASS' if tests_ok else 'FAIL'} "
          f"sample_assertions={'PASS' if samples_ok else 'FAIL'} "
          f"({sum(1 for r in records if r['pass'])}/{len(records)} passed)")
    print(f"[verify] per-case actual results recorded in {RESULTS_FILE}")
    print(f"[verify] overall: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
