#!/usr/bin/env python3
"""Firewall rule change review tool (CLI entry).

Compares an ordered "current" JSON rule set with a "candidate" one and
reports every sub-range of the match space whose effective action changes
(allow->deny or deny->allow), together with the rule that matched before
and after.  It can also check a business traffic list (same range fields
plus an expected action) against the candidate rules and list the
sub-ranges still not satisfied.

Subcommands:
  diff     --current C.json --candidate N.json
  check    --candidate N.json --business B.json
  review   --current C.json --candidate N.json --business B.json
  sample   run the bundled rule-reordering sample (samples/)

Exit codes: 0 = report produced (for check/review: no unsatisfied ranges);
1 = check/review found unsatisfied business ranges; 2 = input/usage error.

Only the Python 3 standard library is used.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import fwmodel

SAMPLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as exc:
        raise fwmodel.ModelError(f"cannot read {path}: {exc}")
    except json.JSONDecodeError as exc:
        raise fwmodel.ModelError(f"{path}: invalid JSON: {exc}")


def load_rules(path):
    return fwmodel.parse_rules(load_json(path))


def load_business(path):
    return fwmodel.parse_business(load_json(path))


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------

def _fmt_range(entry):
    src = entry["src"]
    dst = entry["dst"]
    src = src if isinstance(src, str) else ",".join(src)
    dst = dst if isinstance(dst, str) else ",".join(dst)
    return f"src={src} dst={dst} proto={entry['proto']} port={entry['port']}"


def _fmt_decision(dec):
    rule = dec["rule"] if dec["rule"] is not None else "(default deny)"
    return f"{dec['action']} via {rule}"


def render_text(report):
    lines = []
    if "changes" in report:
        lines.append("== Action changes (sub-ranges whose effective action flips) ==")
        if not report["changes"]:
            lines.append("  (none)")
        for c in report["changes"]:
            lines.append(f"  {_fmt_range(c)}")
            lines.append(f"    before: {_fmt_decision(c['before'])}")
            lines.append(f"    after:  {_fmt_decision(c['after'])}")
        lines.append("")
        lines.append("== Rule-only changes (same action, different matched rule) ==")
        if not report["rule_only_changes"]:
            lines.append("  (none)")
        for c in report["rule_only_changes"]:
            lines.append(f"  {_fmt_range(c)}")
            lines.append(f"    before: {_fmt_decision(c['before'])}")
            lines.append(f"    after:  {_fmt_decision(c['after'])}")
    if "unsatisfied" in report:
        if lines:
            lines.append("")
        lines.append("== Business requirements not satisfied by candidate rules ==")
        if not report["unsatisfied"]:
            lines.append("  (none)")
        for u in report["unsatisfied"]:
            lines.append(f"  business={u['business']} {_fmt_range(u)}")
            rule = u["matched_rule"] if u["matched_rule"] is not None else "(default deny)"
            lines.append(f"    expected={u['expected']} actual={u['actual']} via {rule}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_diff(args):
    current = load_rules(args.current)
    candidate = load_rules(args.candidate)
    report = fwmodel.diff_rulesets(current, candidate)
    emit(report, args.format)
    return 0


def cmd_check(args):
    candidate = load_rules(args.candidate)
    business = load_business(args.business)
    report = {"unsatisfied": fwmodel.check_business(candidate, business)}
    emit(report, args.format)
    return 1 if report["unsatisfied"] else 0


def cmd_review(args):
    current = load_rules(args.current)
    candidate = load_rules(args.candidate)
    business = load_business(args.business)
    report = fwmodel.diff_rulesets(current, candidate)
    report["unsatisfied"] = fwmodel.check_business(candidate, business)
    emit(report, args.format)
    return 1 if report["unsatisfied"] else 0


def cmd_sample(args):
    current = load_rules(os.path.join(SAMPLES_DIR, "reorder_current.json"))
    candidate = load_rules(os.path.join(SAMPLES_DIR, "reorder_candidate.json"))
    business = load_business(os.path.join(SAMPLES_DIR, "reorder_business.json"))
    report = fwmodel.diff_rulesets(current, candidate)
    report["unsatisfied"] = fwmodel.check_business(candidate, business)
    print("# Bundled sample: fine-grained rules moved ahead of a broad rule")
    print("# current   : samples/reorder_current.json")
    print("# candidate : samples/reorder_candidate.json")
    print("# business  : samples/reorder_business.json")
    print()
    emit(report, args.format)
    return 1 if report["unsatisfied"] else 0


def emit(report, fmt):
    if fmt == "json":
        json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        print(render_text(report))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="Review ordered firewall rule changes: show the exact "
                    "sub-ranges whose action flips and the business ranges "
                    "still unsatisfied by the candidate rules.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_format(p):
        p.add_argument("--format", choices=("text", "json"), default="text",
                       help="output format (default: text)")

    p = sub.add_parser("diff", help="compare current vs candidate rule sets")
    p.add_argument("--current", required=True, help="current rules JSON")
    p.add_argument("--candidate", required=True, help="candidate rules JSON")
    add_format(p)
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("check", help="check business traffic against candidate rules")
    p.add_argument("--candidate", required=True, help="candidate rules JSON")
    p.add_argument("--business", required=True, help="business traffic JSON")
    add_format(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("review", help="diff plus business check in one report")
    p.add_argument("--current", required=True, help="current rules JSON")
    p.add_argument("--candidate", required=True, help="candidate rules JSON")
    p.add_argument("--business", required=True, help="business traffic JSON")
    add_format(p)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("sample", help="run the bundled rule-reordering sample")
    add_format(p)
    p.set_defaults(func=cmd_sample)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except fwmodel.ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
