#!/usr/bin/env python3
"""
Local verification wrapper for the precise privilege-escalation re-check.

Usage:
    python tools/local_verify.py --list
    python tools/local_verify.py --case C01
    python tools/local_verify.py --case C01 --case C02   (or --case all)

Each case emits one JSON record per line with the case id, the number of
assertions actually executed, passed/failed/skipped counts, the synthetic
sample (seed) used, and per-assertion expected/observed summaries.  The
process exits non-zero if any assertion fails, if a case is missing, or if
a case executes zero assertions.  All data is synthetic; no credentials or
real account content are used or printed.
"""

import argparse
import json
import os
import sys

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WORKSPACE)

from parliament import analyze_policy_string  # noqa: E402
from parliament.privesc_precheck import precise_recheck  # noqa: E402
from parliament.community_auditors import privilege_escalation  # noqa: E402

# Synthetic docs-style account id; not a real account.
ACCOUNT = "111122223333"
ROLE = "arn:aws:iam::{}:role/".format(ACCOUNT)


class Case:
    def __init__(self, case_id, sample):
        self.record = {
            "case": case_id,
            "sample": sample,
            "assertions": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "details": [],
        }

    def check(self, name, expected, observed):
        ok = expected == observed
        self.record["assertions"] += 1
        self.record["passed" if ok else "failed"] += 1
        self.record["details"].append(
            {
                "name": name,
                "result": "passed" if ok else "failed",
                "expected": expected,
                "observed": observed,
            }
        )
        return ok


def _analyze(policy_dict):
    return analyze_policy_string(json.dumps(policy_dict))


def _stmt(effect, actions, resource, sid=None, condition=None, key="Action"):
    s = {"Effect": effect, key: actions, "Resource": resource}
    if sid:
        s["Sid"] = sid
    if condition:
        s["Condition"] = condition
    return s


# ---------------------------------------------------------------------------
# C01: per-role association of the two steps; no cross-role splicing.
# ---------------------------------------------------------------------------
def case_c01():
    c = Case(
        "C01",
        "policy with ModifyRoleA / AssumeRoleB / FullOnRoleC; roles RoleA, RoleB, RoleC",
    )
    policy = _analyze(
        {
            "Version": "2012-10-17",
            "Statement": [
                _stmt(
                    "Allow",
                    ["iam:AttachRolePolicy", "iam:PutRolePolicy"],
                    ROLE + "RoleA",
                    sid="ModifyRoleA",
                ),
                _stmt(
                    "Allow", "sts:AssumeRole", ROLE + "RoleB", sid="AssumeRoleB"
                ),
                _stmt(
                    "Allow",
                    ["iam:AttachRolePolicy", "iam:PutRolePolicy", "sts:AssumeRole"],
                    ROLE + "RoleC",
                    sid="FullOnRoleC",
                ),
            ],
        }
    )
    result = precise_recheck(
        policy, [ROLE + "RoleA", ROLE + "RoleB", ROLE + "RoleC"]
    )
    roles = result["roles"]

    # RoleA can be modified but not assumed -> blocked, not spliced with RoleB.
    c.check("RoleA status", "blocked", roles[ROLE + "RoleA"]["status"])
    c.check(
        "RoleA modify verdict",
        "allowed",
        roles[ROLE + "RoleA"]["steps"]["modify"]["verdict"],
    )
    c.check(
        "RoleA assume verdict",
        "denied",
        roles[ROLE + "RoleA"]["steps"]["assume"]["verdict"],
    )
    # RoleB can be assumed but not modified -> blocked, not spliced with RoleA.
    c.check("RoleB status", "blocked", roles[ROLE + "RoleB"]["status"])
    c.check(
        "RoleB modify verdict",
        "denied",
        roles[ROLE + "RoleB"]["steps"]["modify"]["verdict"],
    )
    c.check(
        "RoleB assume verdict",
        "allowed",
        roles[ROLE + "RoleB"]["steps"]["assume"]["verdict"],
    )
    # RoleC has both steps on itself -> supported.
    c.check("RoleC status", "supported", roles[ROLE + "RoleC"]["status"])
    # Statement sources stay attached to the correct role/step.
    c.check(
        "RoleA modify allow sources",
        ["ModifyRoleA"],
        roles[ROLE + "RoleA"]["steps"]["modify"]["allow"],
    )
    c.check(
        "RoleB assume allow sources",
        ["AssumeRoleB"],
        roles[ROLE + "RoleB"]["steps"]["assume"]["allow"],
    )
    c.check(
        "RoleC modify allow sources",
        ["FullOnRoleC"],
        roles[ROLE + "RoleC"]["steps"]["modify"]["allow"],
    )
    c.check(
        "RoleC assume allow sources",
        ["FullOnRoleC"],
        roles[ROLE + "RoleC"]["steps"]["assume"]["allow"],
    )
    # No role may become "supported" by crossing RoleA's modify with RoleB's assume.
    c.check(
        "no cross-spliced supported status",
        1,
        sum(1 for r in roles.values() if r["status"] == "supported"),
    )
    return c.record


# ---------------------------------------------------------------------------
# C02: star matching, StringEquals/Bool/Null, missing keys, OR/AND semantics.
# ---------------------------------------------------------------------------
def case_c02():
    c = Case(
        "C02",
        "star actions/resources; StringEquals multi-value; Bool; Null; two-key AND",
    )
    policy = _analyze(
        {
            "Version": "2012-10-17",
            "Statement": [
                _stmt(
                    "Allow",
                    "iam:*RolePolicy",
                    ROLE + "Star*",
                    sid="StarModify",
                ),
                _stmt(
                    "Allow",
                    "sts:AssumeRole",
                    ROLE + "Star1",
                    sid="CondRegion",
                    condition={
                        "StringEquals": {
                            "aws:RequestedRegion": ["us-east-1", "us-west-2"]
                        }
                    },
                ),
                _stmt(
                    "Allow",
                    "sts:AssumeRole",
                    ROLE + "MfaRole",
                    sid="CondMfa",
                    condition={"Bool": {"aws:MultiFactorAuthPresent": "true"}},
                ),
                _stmt(
                    "Allow",
                    "sts:AssumeRole",
                    ROLE + "NullRole",
                    sid="CondNull",
                    condition={"Null": {"aws:PrincipalTag/department": "true"}},
                ),
                _stmt(
                    "Allow",
                    "sts:AssumeRole",
                    ROLE + "AndRole",
                    sid="CondAnd",
                    condition={
                        "StringEquals": {
                            "aws:RequestedRegion": "us-east-1",
                            "aws:PrincipalType": "User",
                        }
                    },
                ),
            ],
        }
    )

    def assume_verdict(role, principal=None, assume_conds=None):
        res = precise_recheck(
            policy,
            [ROLE + role],
            principal_attributes=principal,
            step_conditions={"assume": assume_conds or {}},
        )
        return res["roles"][ROLE + role]["steps"]["assume"]["verdict"]

    def modify_verdict(role):
        res = precise_recheck(policy, [ROLE + role])
        return res["roles"][ROLE + role]["steps"]["modify"]["verdict"]

    # Action star ("iam:*RolePolicy") and Resource star ("Star*") both match.
    c.check("star action+resource modify", "allowed", modify_verdict("Star1"))
    # Same-key values are OR-ed: second alternative us-west-2 satisfies it.
    c.check(
        "StringEquals values OR",
        "allowed",
        assume_verdict("Star1", assume_conds={"aws:RequestedRegion": "us-west-2"}),
    )
    # A region outside the list does not satisfy the condition -> implicit deny.
    c.check(
        "StringEquals non-listed value",
        "denied",
        assume_verdict("Star1", assume_conds={"aws:RequestedRegion": "eu-west-1"}),
    )
    # Bool matches a fixed principal attribute.
    c.check(
        "Bool match",
        "allowed",
        assume_verdict("MfaRole", principal={"aws:MultiFactorAuthPresent": True}),
    )
    # Missing key fails the Bool comparison -> statement inapplicable -> denied.
    c.check("Bool missing key", "denied", assume_verdict("MfaRole"))
    # Null=true is satisfied when the key is absent.
    c.check("Null true with absent key", "allowed", assume_verdict("NullRole"))
    # Null=true fails when the key is present.
    c.check(
        "Null true with present key",
        "denied",
        assume_verdict("NullRole", principal={"aws:PrincipalTag/department": "eng"}),
    )
    # Different keys are AND-ed: only one of two satisfied -> denied.
    c.check(
        "AND keys partial",
        "denied",
        assume_verdict("AndRole", assume_conds={"aws:RequestedRegion": "us-east-1"}),
    )
    # Both keys satisfied -> allowed.
    c.check(
        "AND keys complete",
        "allowed",
        assume_verdict(
            "AndRole",
            assume_conds={
                "aws:RequestedRegion": "us-east-1",
                "aws:PrincipalType": "User",
            },
        ),
    )
    return c.record


# ---------------------------------------------------------------------------
# C03: explicit Deny precedence and three-valued unknown handling.
# ---------------------------------------------------------------------------
def case_c03():
    c = Case(
        "C03",
        "explicit Deny vs Allow; unsupported condition operator; NotAction/NotResource",
    )
    policy = _analyze(
        {
            "Version": "2012-10-17",
            "Statement": [
                _stmt(
                    "Allow",
                    ["iam:AttachRolePolicy", "iam:PutRolePolicy", "sts:AssumeRole"],
                    ROLE + "Allowed*",
                    sid="AllowAll",
                ),
                _stmt(
                    "Deny", "sts:AssumeRole", ROLE + "AllowedDenied", sid="DenyAssume"
                ),
                _stmt(
                    "Allow",
                    "sts:AssumeRole",
                    ROLE + "AllowedDenied",
                    sid="UnknownAllowExtra",
                    condition={
                        "NumericLessThan": {"aws:MultiFactorAuthAge": "300"}
                    },
                ),
                _stmt(
                    "Deny",
                    "sts:AssumeRole",
                    ROLE + "AllowedMaybe",
                    sid="UnknownDenyCond",
                    condition={"IpAddress": {"aws:SourceIp": "203.0.113.0/24"}},
                ),
            ],
        }
    )
    result = precise_recheck(policy, [ROLE + "AllowedDenied", ROLE + "AllowedMaybe"])
    roles = result["roles"]

    denied_assume = roles[ROLE + "AllowedDenied"]["steps"]["assume"]
    # Applicable explicit Deny wins over the Allow.
    c.check("explicit deny verdict", "denied", denied_assume["verdict"])
    c.check("explicit deny source", ["DenyAssume"], denied_assume["deny"])
    # The determinate deny is not washed out by the unrelated unknown statement.
    c.check("denied role status", "blocked", roles[ROLE + "AllowedDenied"]["status"])
    c.check(
        "unknown statement still recorded",
        ["UnknownAllowExtra"],
        [u["source"] for u in denied_assume["unknown"]],
    )

    maybe_assume = roles[ROLE + "AllowedMaybe"]["steps"]["assume"]
    # A possibly-applicable unknown Deny is not masked by the known Allow.
    c.check("unknown deny verdict", "unknown", maybe_assume["verdict"])
    c.check(
        "unknown deny source",
        ["UnknownDenyCond"],
        [u["source"] for u in maybe_assume["unknown"] if u["effect"] == "Deny"],
    )
    c.check("maybe role status", "undetermined", roles[ROLE + "AllowedMaybe"]["status"])

    # NotAction / NotResource are kept unknown: neither allowed nor ignored.
    policy2 = _analyze(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "NotActionAllow",
                    "Effect": "Allow",
                    "NotAction": "s3:*",
                    "Resource": ROLE + "NaRole",
                },
                {
                    "Sid": "NotResourceDeny",
                    "Effect": "Deny",
                    "Action": "sts:AssumeRole",
                    "NotResource": ROLE + "NrExcluded",
                },
            ],
        }
    )
    res2 = precise_recheck(policy2, [ROLE + "NaRole", ROLE + "NrExcluded"])
    na = res2["roles"][ROLE + "NaRole"]
    # NotAction allow is unknown, not an unconditional allow, and not ignored.
    c.check("NotAction modify verdict", "unknown", na["steps"]["modify"]["verdict"])
    c.check(
        "NotAction unknown sources",
        ["NotActionAllow"],
        [u["source"] for u in na["steps"]["modify"]["unknown"]],
    )
    c.check("NotAction role status", "undetermined", na["status"])
    # Role excluded by NotResource: the deny definitely does not apply.
    nr = res2["roles"][ROLE + "NrExcluded"]["steps"]["assume"]
    c.check("NotResource excluded verdict", "denied", nr["verdict"])
    c.check("NotResource excluded unknowns", [], nr["unknown"])
    # Role not excluded by NotResource: the deny may apply -> unknown deny.
    res3 = precise_recheck(policy2, [ROLE + "NrOther"])
    nro = res3["roles"][ROLE + "NrOther"]["steps"]["assume"]
    c.check("NotResource applicable verdict", "unknown", nro["verdict"])
    c.check(
        "NotResource applicable unknown source",
        ["NotResourceDeny"],
        [u["source"] for u in nro["unknown"]],
    )
    return c.record


# ---------------------------------------------------------------------------
# C04: per-role/per-step statement sources and default interface compatibility.
# ---------------------------------------------------------------------------
def case_c04():
    c = Case(
        "C04",
        "split-statement allows across roles; default audit interface unchanged",
    )
    policy_str = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                _stmt("Allow", "iam:AttachRolePolicy", ROLE + "Split", sid="S1"),
                _stmt("Allow", "iam:PutRolePolicy", ROLE + "Split", sid="S2"),
                _stmt("Allow", "sts:AssumeRole", ROLE + "Split", sid="S3"),
                _stmt("Allow", "sts:AssumeRole", ROLE + "Other", sid="S4"),
            ],
        }
    )
    # Default audit interface still works and still raises the combo finding.
    policy = analyze_policy_string(policy_str, include_community_auditors=True)
    escalations = [
        f
        for f in policy.findings
        if f.issue == "PRIVILEGE_ESCALATION"
    ]
    c.check("default audit still finds escalation", True, len(escalations) > 0)
    c.check(
        "default audit location types",
        ["AttachRolePolicy", "PutRolePolicy"],
        sorted(f.location.get("type") for f in escalations),
    )
    c.check("audit callable", True, callable(privilege_escalation.audit))
    c.check(
        "precise_recheck exposed on auditor",
        True,
        callable(privilege_escalation.precise_recheck),
    )

    # Precise re-check via the auditor's optional entry point.
    result = privilege_escalation.precise_recheck(
        policy, [ROLE + "Split", ROLE + "Other"]
    )
    roles = result["roles"]
    split = roles[ROLE + "Split"]
    c.check("split role status", "supported", split["status"])
    # Authorization spread across multiple statements is attributed per step.
    c.check("split modify sources", ["S1", "S2"], split["steps"]["modify"]["allow"])
    c.check("split assume sources", ["S3"], split["steps"]["assume"]["allow"])
    c.check(
        "attach action source",
        ["S1"],
        split["steps"]["modify"]["actions"]["iam:AttachRolePolicy"]["allow"],
    )
    c.check(
        "put action source",
        ["S2"],
        split["steps"]["modify"]["actions"]["iam:PutRolePolicy"]["allow"],
    )
    # The other role only has the assume step -> blocked, sources stay separate.
    other = roles[ROLE + "Other"]
    c.check("other role status", "blocked", other["status"])
    c.check("other assume sources", ["S4"], other["steps"]["assume"]["allow"])
    c.check("other modify sources", [], other["steps"]["modify"]["allow"])

    # The result is scoped to the identity policy and must not masquerade as a
    # trust-policy or organization-policy exploitability conclusion.
    c.check("scope note present", True, bool(result.get("scope")))
    c.check(
        "scope note mentions identity-policy-only",
        True,
        "identity-policy-only" in result.get("scope", "").lower()
        or "identity policy" in result.get("scope", "").lower(),
    )
    c.check(
        "scope note disclaims trust policy",
        True,
        "trust polic" in result.get("scope", "").lower(),
    )
    c.check(
        "status vocabulary",
        ["blocked", "supported", "undetermined"],
        sorted({r["status"] for r in roles.values()} | {"supported", "blocked", "undetermined"}),
    )
    c.check(
        "statuses within vocabulary",
        True,
        all(
            r["status"] in ("supported", "blocked", "undetermined")
            for r in roles.values()
        ),
    )
    return c.record


CASES = {
    "C01": case_c01,
    "C02": case_c02,
    "C03": case_c03,
    "C04": case_c04,
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list available cases")
    parser.add_argument(
        "--case",
        action="append",
        help="case id to run (C01..C04 or 'all'); may be repeated",
    )
    args = parser.parse_args(argv)

    if args.list:
        for case_id in sorted(CASES):
            print(case_id)
        return 0

    if not args.case:
        parser.error("specify --list or at least one --case")

    requested = sorted(CASES) if "all" in args.case else args.case
    exit_code = 0
    for case_id in requested:
        if case_id not in CASES:
            print(
                json.dumps(
                    {"case": case_id, "error": "unknown case"}, sort_keys=True
                )
            )
            exit_code = 1
            continue
        record = CASES[case_id]()
        if record["assertions"] == 0 or record["failed"] > 0:
            exit_code = 1
        if record["assertions"] > 0 and record["passed"] == 0:
            # Entire group skipped or never actually asserted.
            exit_code = 1
        print(json.dumps(record, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
