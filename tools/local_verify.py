#!/usr/bin/env python3
"""Local verification entry point for SEC-VULN-039.

This wrapper does NOT trust unittest's zero-test exit code (which is 0):

    1. discovers the suite via unittest.defaultTestLoader.discover("tests")
       and asserts countTestCases() >= 1;
    2. runs the suite and asserts that at least one test actually executed
       and was not skipped, and that nothing failed/errored;
    3. executes the delivered sample/boundary assertions directly against
       the synthetic fixtures, recording every actual result alongside its
       expected value;
    4. exits non-zero on zero tests, all-skipped suites, exceptions, any
       failed assertion, or any sample mismatch.

Standard library only.  Run with the approved interpreter:
    /usr/local/bin/python3 tools/local_verify.py
"""

import io
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from acl_diag.evaluator import (  # noqa: E402
    ALLOW,
    DENY,
    UNVERIFIED,
    MODIFY_FILE,
    REPLACE_ENTRY,
    evaluate_inventory,
    evaluate_subject,
    load_inventory,
    load_snapshot,
)
from acl_diag.membership import expand_members, load_subject_directory  # noqa: E402
from acl_diag.diff import build_diff  # noqa: E402
from acl_diag.remediation import remediate  # noqa: E402

EXAMPLE = os.path.join(REPO_ROOT, "examples", "scenario_a")


def _load_case_inputs(version_file):
    inventory = load_inventory(os.path.join(EXAMPLE, "inventory.json"))
    subjects = load_subject_directory(os.path.join(EXAMPLE, version_file))
    snapshot = load_snapshot(os.path.join(EXAMPLE, "snapshot.json"))
    return inventory, subjects, snapshot


def _finding(inventory, subjects, snapshot, service, path, subject):
    return evaluate_subject(service, path, subject, subjects, snapshot)


def sample_assertions():
    """Stable case_id -> assertion record with actual vs expected values."""

    records = []

    def record(case_id, description, actual, expected):
        passed = actual == expected
        records.append(
            {
                "case_id": case_id,
                "description": description,
                "input": "examples/scenario_a synthetic fixtures",
                "expected": expected,
                "actual": actual,
                "passed": passed,
            }
        )

    inv_v1, sub_v1, snap = _load_case_inputs("subjects_v1.json")
    _, sub_v2, _ = _load_case_inputs("subjects_v2.json")

    # CASE-01: group membership change removes the inherited write path.
    before = expand_members("acc_bob", sub_v1)
    after = expand_members("acc_bob", sub_v2)
    record(
        "CASE-01-membership-change",
        "bob 属于 grp_legacy_share（v1）；v2 移除后不再匹配该组",
        {
            "v1_member_of_legacy_share": "grp_legacy_share" in before,
            "v2_member_of_legacy_share": "grp_legacy_share" in after,
            "v2_modify_decision": _finding(
                inv_v1, sub_v2, snap,
                "svc_billing", "/srv/fictional/billing/billing.conf", "acc_bob",
            ).results[MODIFY_FILE].decision,
        },
        {
            "v1_member_of_legacy_share": True,
            "v2_member_of_legacy_share": False,
            "v2_modify_decision": UNVERIFIED,
        },
    )

    # CASE-02: explicit deny overrides inherited allow.
    auth = _finding(
        inv_v1, sub_v1, snap,
        "svc_auth", "/srv/fictional/auth/authd.yaml", "acc_carol",
    )
    record(
        "CASE-02-explicit-deny-over-inherited-allow",
        "carol 对 authd.yaml：继承 allow 被文件上的显式 deny 覆盖；"
        "父目录项替换仍为 allow",
        {
            "modify_file": auth.results[MODIFY_FILE].decision,
            "replace_entry": auth.results[REPLACE_ENTRY].decision,
        },
        {"modify_file": DENY, "replace_entry": ALLOW},
    )

    # CASE-03: read-only file but the directory entry is replaceable.
    router = _finding(
        inv_v1, sub_v1, snap,
        "svc_router", "/srv/fictional/router/routes.conf", "acc_alice",
    )
    record(
        "CASE-03-readonly-file-replaceable-entry",
        "alice 不能改 routes.conf 内容，但可通过父目录替换条目",
        {
            "modify_file": router.results[MODIFY_FILE].decision,
            "replace_entry": router.results[REPLACE_ENTRY].decision,
        },
        {"modify_file": DENY, "replace_entry": ALLOW},
    )

    # CASE-04: directory permissions not exported -> unverified.
    ledger = _finding(
        inv_v1, sub_v1, snap,
        "svc_ledger", "/srv/fictional/ledger/ledger.ini", "acc_bob",
    )
    record(
        "CASE-04-unexported-directory",
        "ledger 父目录 exported=false：replace_entry 必须为待核实",
        {
            "modify_file": ledger.results[MODIFY_FILE].decision,
            "replace_entry": ledger.results[REPLACE_ENTRY].decision,
            "parent_exported": ledger.results[REPLACE_ENTRY].object_exported,
        },
        {
            "modify_file": ALLOW,
            "replace_entry": UNVERIFIED,
            "parent_exported": False,
        },
    )

    # CASE-05: missing file and unresolved subject never become implicit deny.
    ghost = _finding(
        inv_v1, sub_v1, snap,
        "svc_ghost", "/srv/fictional/missing/ghost.conf", "acc_bob",
    )
    stranger = _finding(
        inv_v1, sub_v1, snap,
        "svc_billing", "/srv/fictional/billing/billing.conf", "acc_stranger",
    )
    record(
        "CASE-05-missing-records-stay-unverified",
        "快照缺失路径、未知主体均判待核实，而不是默认 allow/deny",
        {
            "missing_file_decision": ghost.results[MODIFY_FILE].decision,
            "unknown_subject_decision": stranger.results[MODIFY_FILE].decision,
            "unknown_subject_resolved": stranger.subject_resolved,
        },
        {
            "missing_file_decision": UNVERIFIED,
            "unknown_subject_decision": UNVERIFIED,
            "unknown_subject_resolved": False,
        },
    )

    # CASE-06: diff output flips the legacy periodic-check conclusions.
    findings = evaluate_inventory(inv_v1, sub_v1, snap)
    rows = build_diff(inv_v1, findings)
    index = {
        (r["service"], r["subject_id"], r["action"]): r for r in rows
    }
    record(
        "CASE-06-legacy-vs-new-diff",
        "新旧结论差异：auth 由 allow 翻转为 deny；router 替换由 deny 翻转为 allow；"
        "ledger 替换变为待核实",
        {
            "auth_modify_changed": index[
                ("svc_auth", "acc_carol", MODIFY_FILE)
            ]["new_conclusion"],
            "router_replace_changed": index[
                ("svc_router", "acc_alice", REPLACE_ENTRY)
            ]["new_conclusion"],
            "ledger_replace_new": index[
                ("svc_ledger", "acc_bob", REPLACE_ENTRY)
            ]["new_conclusion"],
            "diff_row_count": len(rows),
        },
        {
            "auth_modify_changed": DENY,
            "router_replace_changed": ALLOW,
            "ledger_replace_new": UNVERIFIED,
            "diff_row_count": 8,
        },
    )

    # CASE-07: remediation closes every ordinary-account allow and re-verifies.
    plan, fixed_snapshot, recheck = remediate(inv_v1, sub_v1, snap, findings)
    residual = [r for r in recheck if r["still_allowed"]]
    post = {
        (r["service"], r["subject_id"], r["action"]): r["post_fix_decision"]
        for r in recheck
    }
    record(
        "CASE-07-remediation-and-reverification",
        "最小整改后普通账号不再有 allow；billing 两项均显式 deny；"
        "未导出目录保持待核实",
        {
            "residual_allow_count": len(residual),
            "billing_bob_modify": post[
                ("svc_billing", "acc_bob", MODIFY_FILE)
            ],
            "billing_bob_replace": post[
                ("svc_billing", "acc_bob", REPLACE_ENTRY)
            ],
            "ledger_bob_replace": post[
                ("svc_ledger", "acc_bob", REPLACE_ENTRY)
            ],
            "unresolved_gaps_recorded": len(plan.unresolved) > 0,
        },
        {
            "residual_allow_count": 0,
            "billing_bob_modify": DENY,
            "billing_bob_replace": DENY,
            "ledger_bob_replace": UNVERIFIED,
            "unresolved_gaps_recorded": True,
        },
    )

    return records


def run_unit_suite():
    loader = unittest.defaultTestLoader
    suite = loader.discover("tests")
    discovered = suite.countTestCases()
    if discovered < 1:
        return None, {
            "discovered": 0,
            "executed": 0,
            "skipped": 0,
            "failures": 0,
            "errors": 0,
            "fatal": "discover('tests') returned zero test cases",
        }

    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2)
    result = runner.run(suite)

    skipped = len(getattr(result, "skipped", []))
    executed = discovered - skipped
    metrics = {
        "discovered": discovered,
        "executed": executed,
        "skipped": skipped,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "fatal": None,
        "runner_output": stream.getvalue(),
    }
    if executed < 1:
        metrics["fatal"] = "all discovered tests were skipped"
    if result.failures or result.errors:
        metrics["fatal"] = "unit suite reported failures or errors"
    return result, metrics


def main():
    os.chdir(REPO_ROOT)
    report = {"tool": "SEC-VULN-039-local-verify", "stage": "unit_tests"}

    _, metrics = run_unit_suite()
    report["unit_tests"] = {k: v for k, v in metrics.items() if k != "runner_output"}
    print(metrics.get("runner_output", ""))

    report["stage"] = "sample_assertions"
    samples = sample_assertions()
    report["sample_assertions"] = [
        {k: v for k, v in row.items()} for row in samples
    ]
    sample_failures = [r["case_id"] for r in samples if not r["passed"]]

    print("=" * 72)
    print("Sample / boundary assertions (delivered fixtures):")
    for row in samples:
        print(
            "  [%s] %s -> passed=%s"
            % (row["case_id"], row["description"], row["passed"])
        )
        print("      expected=%s" % json.dumps(row["expected"], ensure_ascii=False))
        print("      actual  =%s" % json.dumps(row["actual"], ensure_ascii=False))
    print("=" * 72)

    ok = metrics["fatal"] is None and not sample_failures
    report["overall_passed"] = ok
    report_path = os.path.join(REPO_ROOT, "tools", "local_verify_report.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    if not ok:
        if metrics["fatal"]:
            print("FAIL: %s" % metrics["fatal"], file=sys.stderr)
        for case_id in sample_failures:
            print("FAIL: sample assertion %s mismatch" % case_id, file=sys.stderr)
        print("report written to %s" % report_path, file=sys.stderr)
        return 1

    print(
        "PASS: %d unit tests executed (%d skipped), %d sample assertions held"
        % (metrics["executed"], metrics["skipped"], len(samples))
    )
    print("report written to %s" % report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
