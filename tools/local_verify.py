#!/usr/bin/env python3
"""Local verification wrapper for SEC-20261114-045.

Run with:  /usr/local/bin/python3 tools/local_verify.py

Step 1 discovers the unit test suite via
unittest.defaultTestLoader.discover("tests"), refuses to pass when zero tests
are discovered, then actually runs the suite and refuses to pass when no test
was really executed (all skipped) or when anything failed/errored.

Step 2 runs the delivered sample/boundary assertions against the real CLI and
the real auditor core, printing expected vs. actual results per case.

Exit code 0 only when both steps pass; any failure, error, zero-test or
all-skipped situation exits non-zero.
"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")
APP = os.path.join(ROOT, "app.py")


def run_unit_tests():
    print("== step 1: unit tests ==")
    os.chdir(ROOT)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    total = suite.countTestCases()
    print("[verify] discovered %d unit test case(s) in tests/" % total)
    if total < 1:
        print("[verify] FAIL: zero tests discovered")
        return False
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    skipped = len(result.skipped)
    executed = result.testsRun - skipped
    print("[verify] testsRun=%d skipped=%d executed=%d failures=%d errors=%d"
          % (result.testsRun, skipped, executed,
             len(result.failures), len(result.errors)))
    if executed < 1:
        print("[verify] FAIL: no test actually executed (zero or all skipped)")
        return False
    if not result.wasSuccessful():
        print("[verify] FAIL: unit tests reported failures/errors")
        return False
    print("[verify] unit tests: PASS")
    return True


def _audit_file(path):
    sys.path.insert(0, ROOT) if ROOT not in sys.path else None
    import pageaudit
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return pageaudit.audit_document(doc)


def _page_summary(build_report):
    pages = build_report.get("affected_pages") or []
    if not pages:
        return None
    page = pages[0]
    return {k: page[k] for k in
            ("page_index", "effective_perms", "contributing_segments",
             "applied_rule")}


def run_sample_assertions():
    print("== step 2: sample/boundary assertions ==")
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import pageaudit

    results = []

    def record(case_id, description, expected, actual):
        ok = expected == actual
        results.append(ok)
        print("[case %s] %s" % (case_id, description))
        print("  expected: %s" % json.dumps(expected, sort_keys=True))
        print("  actual:   %s" % json.dumps(actual, sort_keys=True))
        print("  result:   %s" % ("PASS" if ok else "FAIL"))

    # SAMPLE-UNION-001: the two small mappings, union rule -> page is W+X.
    report = _audit_file(os.path.join(SAMPLES, "mapping_union.json"))
    br = report["build_reports"][0]
    record(
        "SAMPLE-UNION-001",
        "disjoint segments aligned onto one page, union rule -> page is W+X",
        {"status": "ok", "affected_page_count": 1,
         "page": {"page_index": 1, "effective_perms": "rwx",
                  "contributing_segments": ["code", "data"],
                  "applied_rule": "union"}},
        {"status": br["status"],
         "affected_page_count": br.get("affected_page_count"),
         "page": _page_summary(br)})

    # SAMPLE-LAST-001: same layout, last_loaded rule -> page is not W+X.
    report = _audit_file(os.path.join(SAMPLES, "mapping_last_loaded.json"))
    br = report["build_reports"][0]
    record(
        "SAMPLE-LAST-001",
        "same segments, last_loaded rule -> last segment is rw-, not W+X",
        {"status": "ok", "affected_page_count": 0},
        {"status": br["status"],
         "affected_page_count": br.get("affected_page_count")})

    # ERR-ADDR-001: address beyond the 64-bit space is flagged.
    doc = {"builds": [{"build_id": "b", "page_size": 4096,
                       "merge_rule": "union",
                       "segments": [{"name": "s", "start": 1 << 64,
                                     "end": (1 << 64) + 1,
                                     "perms": "r-x"}]}]}
    br = pageaudit.audit_document(doc)["build_reports"][0]
    record(
        "ERR-ADDR-001",
        "address 2^64 cannot be represented -> build flagged",
        {"status": "error", "error_type": "address_unrepresentable"},
        {"status": br["status"],
         "error_type": br.get("error", {}).get("type")})

    # ERR-PAGE-001: page size that is not a power of two is flagged.
    doc = {"builds": [{"build_id": "b", "page_size": 1000,
                       "merge_rule": "union",
                       "segments": [{"name": "s", "start": "0x1000",
                                     "end": "0x2000", "perms": "r-x"}]}]}
    br = pageaudit.audit_document(doc)["build_reports"][0]
    record(
        "ERR-PAGE-001",
        "page_size 1000 is not a power of two -> build flagged",
        {"status": "error", "error_type": "invalid_page_size"},
        {"status": br["status"],
         "error_type": br.get("error", {}).get("type")})

    # ERR-MISSING-001: build without mapping records is flagged.
    doc = {"builds": [{"build_id": "b", "page_size": 4096,
                       "merge_rule": "union"}]}
    br = pageaudit.audit_document(doc)["build_reports"][0]
    record(
        "ERR-MISSING-001",
        "build without segment records -> build flagged",
        {"status": "error", "error_type": "missing_mapping_records"},
        {"status": br["status"],
         "error_type": br.get("error", {}).get("type")})

    # BOUNDARY-MAX-001: top of the 64-bit address space still works.
    doc = {"builds": [{"build_id": "b", "page_size": 4096,
                       "merge_rule": "union",
                       "segments": [{"name": "top",
                                     "start": "0xfffffffffffff000",
                                     "end": "0xffffffffffffffff",
                                     "perms": "rwx"}]}]}
    br = pageaudit.audit_document(doc)["build_reports"][0]
    record(
        "BOUNDARY-MAX-001",
        "segment at the top of the 64-bit space -> last page flagged W+X",
        {"status": "ok", "page_index": (1 << 64) // 4096 - 1},
        {"status": br["status"],
         "page_index": (br.get("affected_pages") or [{}])[0].get("page_index")})

    # CLI-MIXED-001: real CLI run over the mixed sample.
    proc = subprocess.run(
        [sys.executable, APP, os.path.join(SAMPLES, "mixed_builds.json")],
        capture_output=True, text=True, timeout=30)
    actual = {"returncode": proc.returncode}
    if proc.returncode == 0:
        try:
            report = json.loads(proc.stdout)
            actual["summary"] = report["summary"]
            actual["error_types"] = sorted(
                r["error"]["type"] for r in report["build_reports"]
                if r["status"] == "error")
        except ValueError:
            actual["stdout_parse"] = "failed"
    record(
        "CLI-MIXED-001",
        "CLI over mixed sample: 1 ok build, 3 flagged builds, 1 W+X page",
        {"returncode": 0,
         "summary": {"total_builds": 4, "ok_builds": 1, "error_builds": 3,
                     "total_affected_pages": 1},
         "error_types": ["address_unrepresentable", "invalid_page_size",
                         "missing_mapping_records"]},
        actual)

    passed = sum(1 for ok in results if ok)
    print("[verify] sample/boundary assertions: %d/%d PASS"
          % (passed, len(results)))
    return all(results)


def main():
    print("== SEC-20261114-045 local verification ==")
    ok_unit = run_unit_tests()
    ok_cases = run_sample_assertions()
    if ok_unit and ok_cases:
        print("[verify] OVERALL: PASS")
        return 0
    print("[verify] OVERALL: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
