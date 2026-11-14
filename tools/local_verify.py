#!/usr/bin/env python3
"""Local verification wrapper for SEC-VULN-043 (stdlib only).

Steps:
  1. Discover the unittest suite via
     unittest.defaultTestLoader.discover("tests"); require >= 1 case.
  2. Actually run the suite; require >= 1 executed, non-skipped test and
     zero failures/errors.
  3. Run the delivered scenario suite and boundary assertions, printing
     each actual result next to its expectation.

Exit code 0 only if every step passes. Run with:
    /usr/local/bin/python3 tools/local_verify.py
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_unittest_suite():
    os.chdir(ROOT)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    count = suite.countTestCases()
    print(f"[verify] discovered {count} test case(s) under tests/")
    if count < 1:
        print("[verify] FAIL: zero tests discovered")
        return False
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    skipped = len(result.skipped)
    executed = result.testsRun - skipped
    print(f"[verify] testsRun={result.testsRun} executed={executed} "
          f"skipped={skipped} failures={len(result.failures)} "
          f"errors={len(result.errors)}")
    if executed < 1:
        print("[verify] FAIL: no test actually executed "
              "(zero tests or all skipped)")
        return False
    if result.failures or result.errors:
        print("[verify] FAIL: unittest suite had failures/errors")
        return False
    return True


def run_case_assertions():
    from scenarios import run_all
    ok = True
    for case in run_all():
        export_id = (case.export_record["export_id"]
                     if case.export_record else None)
        rules = ",".join(f"{r['rule']}:{r['status']}"
                         for r in case.rules_triggered)
        print(f"[case] {case.case_id}: expected={case.expected} "
              f"actual={case.decision} export={export_id} "
              f"export_count={case.export_count} rules=[{rules}] "
              f"-> {'OK' if case.ok else 'MISMATCH'}")
        ok = ok and case.ok
    return ok


def run_boundary_assertions():
    from approval_core import canonical_params
    from scenarios import BASE_PARAMS, NOW, TENANT_A, approve, build_env
    from approval_core import OP_BATCH_EXPORT, Operation
    from approval_core import ROLE_AUDITOR, ROLE_DATA_OWNER

    checks = []

    actual = (canonical_params({"format": "csv", "tables": ["alpha", "beta"]})
              == canonical_params({"tables": ["beta", "alpha"],
                                   "format": "csv"}))
    checks.append(("B1_param_order_normalizes_equal", actual, True))

    actual = (canonical_params({"format": "csv", "tables": ["alpha", "beta"]})
              != canonical_params({"format": "csv",
                                   "tables": ["alpha", "gamma"]}))
    checks.append(("B2_param_content_change_detected", actual, True))

    # B3: expiry boundary is exclusive (expires_at == now is invalid).
    ids, registry, store, service = build_env()
    registry.register(TENANT_A, "res-1", 7)
    approve(ids, store, "appr-b3-owner", "alice", ROLE_DATA_OWNER,
            TENANT_A, "res-1", 7, BASE_PARAMS, expires_at=NOW)
    approve(ids, store, "appr-b3-auditor", "bob", ROLE_AUDITOR,
            TENANT_A, "res-1", 7, BASE_PARAMS, expires_at=NOW)
    op = Operation(TENANT_A, "res-1", 7, OP_BATCH_EXPORT, dict(BASE_PARAMS))
    outcome = service.execute(op, NOW)
    checks.append(("B3_expiry_boundary_exclusive", outcome.approved, False))
    checks.append(("B3_no_export_on_expired", len(service.exports), 0))

    ok = True
    for name, actual, expected in checks:
        passed = actual == expected
        ok = ok and passed
        print(f"[boundary] {name}: actual={actual} expected={expected} "
              f"-> {'OK' if passed else 'FAIL'}")
    return ok


def main():
    ok = True
    ok = run_unittest_suite() and ok
    ok = run_case_assertions() and ok
    ok = run_boundary_assertions() and ok
    print(f"[verify] overall: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
