#!/usr/bin/env python3
"""SEC-VULN-039 offline diagnostic CLI.

Usage:
    python3 app.py analyze    --inventory INV --subjects SUBJ --snapshot SNAP
                              [--output OUT]
    python3 app.py diff       --inventory INV --subjects SUBJ --snapshot SNAP
                              [--output OUT]
    python3 app.py remediate  --inventory INV --subjects SUBJ --snapshot SNAP
                              [--fixed-snapshot OUT] [--output OUT]

All inputs are synthetic JSON records.  Nothing is read from or written
to a real service host: the tool only parses the supplied files and
emits JSON reports.

Exit codes:
    0  analysis completed (use --fail-on-risk to make residual ALLOW
       findings for ordinary accounts fail the run with code 1)
    1  --fail-on-risk triggered
    2  invalid arguments / unreadable or malformed input
"""

import argparse
import json
import os
import sys
from typing import List

# Make the local package importable even under "python -I" (which strips
# PYTHONPATH and the current working directory from sys.path).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from acl_diag.models import ALLOW
from acl_diag.evaluator import (
    evaluate_inventory,
    load_inventory,
    load_snapshot,
)
from acl_diag.membership import load_subject_directory
from acl_diag.diff import build_diff
from acl_diag.remediation import remediate, snapshot_to_dict


PRIORITY_POLICY = [
    "1. explicit deny (highest)",
    "2. explicit allow",
    "3. inherited deny",
    "4. inherited allow (lowest)",
    "Missing object, unexported object (exported=false), unresolved "
    "subject, or no applicable ACE -> unverified (待核实); no default "
    "allow or deny is assumed for missing records.",
]


def _load_all(args):
    inventory = load_inventory(args.inventory)
    directory = load_subject_directory(args.subjects)
    snapshot = load_snapshot(args.snapshot)
    return inventory, directory, snapshot


def _emit(payload: dict, output: str = None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
    if output:
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        print(text)


def _has_residual_risk(recheck_rows: List[dict]) -> bool:
    return any(row["still_allowed"] for row in recheck_rows)


def cmd_analyze(args) -> int:
    inventory, directory, snapshot = _load_all(args)
    findings = evaluate_inventory(inventory, directory, snapshot)
    payload = {
        "tool": "SEC-VULN-039-acl-diagnostic",
        "priority_policy": PRIORITY_POLICY,
        "inputs": {
            "inventory": args.inventory,
            "subjects": args.subjects,
            "snapshot": args.snapshot,
        },
        "findings": [f.to_dict() for f in findings],
    }
    _emit(payload, args.output)
    return 0


def cmd_diff(args) -> int:
    inventory, directory, snapshot = _load_all(args)
    findings = evaluate_inventory(inventory, directory, snapshot)
    rows = build_diff(inventory, findings)
    payload = {
        "tool": "SEC-VULN-039-acl-diagnostic",
        "priority_policy": PRIORITY_POLICY,
        "diff": rows,
        "summary": {
            "total": len(rows),
            "changed": sum(1 for r in rows if r["changed"]),
            "newly_allow": sum(
                1 for r in rows if r["changed"] and r["new_conclusion"] == ALLOW
            ),
        },
    }
    _emit(payload, args.output)
    return 0


def cmd_remediate(args) -> int:
    inventory, directory, snapshot = _load_all(args)
    findings = evaluate_inventory(inventory, directory, snapshot)
    plan, fixed, recheck = remediate(inventory, directory, snapshot, findings)
    if args.fixed_snapshot:
        with open(args.fixed_snapshot, "w", encoding="utf-8") as handle:
            json.dump(snapshot_to_dict(fixed), handle,
                      ensure_ascii=False, indent=2)
            handle.write("\n")
    payload = {
        "tool": "SEC-VULN-039-acl-diagnostic",
        "priority_policy": PRIORITY_POLICY,
        "plan": plan.to_dict(),
        "fixed_snapshot_written_to": args.fixed_snapshot,
        "fixed_snapshot": snapshot_to_dict(fixed),
        "reverification": recheck,
        "residual_allow_for_ordinary_accounts": _has_residual_risk(recheck),
    }
    _emit(payload, args.output)
    if args.fail_on_risk and _has_residual_risk(recheck):
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline ACL diagnostic for migrated service configurations "
                    "(synthetic data only)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--inventory", required=True,
                       help="service configuration inventory JSON")
        p.add_argument("--subjects", required=True,
                       help="subject/group relationship JSON")
        p.add_argument("--snapshot", required=True,
                       help="directory permission snapshot JSON")
        p.add_argument("--output", help="write JSON report here instead of stdout")

    p_analyze = sub.add_parser("analyze", help="evaluate modify/replace rights")
    add_common(p_analyze)
    p_analyze.set_defaults(func=cmd_analyze)

    p_diff = sub.add_parser("diff", help="legacy vs new conclusions")
    add_common(p_diff)
    p_diff.set_defaults(func=cmd_diff)

    p_rem = sub.add_parser("remediate", help="minimal fixes + re-verification")
    add_common(p_rem)
    p_rem.add_argument("--fixed-snapshot",
                       help="write the remediated permission snapshot JSON here")
    p_rem.add_argument("--fail-on-risk", action="store_true",
                       help="exit 1 if any ordinary account still has an allow "
                            "after remediation")
    p_rem.set_defaults(func=cmd_remediate)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except json.JSONDecodeError as exc:
        print("error: malformed JSON input: %s" % exc, file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
