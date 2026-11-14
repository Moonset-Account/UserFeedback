#!/usr/bin/env python3
"""CLI entry for the two-step historical chat attachment cleanup.

Usage:
  python3 app.py demo    --data DIR                      # write a sample dataset
  python3 app.py plan    --data DIR [--now YYYY-MM-DD]   # step 1: confirm list
  python3 app.py execute --data DIR [--now YYYY-MM-DD]   # step 2: run cleanup
  python3 app.py status  --data DIR                      # show journal summary
"""

import argparse
import datetime
import json
import os
import sys

import cleanup_core as core


def _parse_now(value):
    if value is None:
        return datetime.date.today()
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"error: --now must be an ISO date (YYYY-MM-DD), got {value!r}")


def cmd_plan(args):
    now = _parse_now(args.now)
    entries = core.plan_confirmation(args.data, now)
    print(f"confirmed {len(entries)} attachment(s) -> "
          f"{os.path.join(args.data, core.CONFIRMED_FILE)}")
    for entry in entries:
        print(f"  {entry['attachment_id']} case={entry['case_id']} "
              f"space={entry['space']} confirmed_version={entry['confirmed_version']}")
    return 0


def cmd_execute(args):
    now = _parse_now(args.now)
    try:
        result = core.execute_confirmed(args.data, now)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    counts = result["counts"]
    print(f"deleted={counts['deleted']} skipped={counts['skipped']} "
          f"pending_retry={counts['pending_retry']} pending={counts['pending']}")
    for outcome in result["outcomes"]:
        reason = f" reason={outcome['reason']}" if outcome.get("reason") else ""
        source = " (journal)" if outcome.get("from_journal") else ""
        print(f"  {outcome['attachment_id']}: {outcome['status']}{reason}{source}")
    print(f"result written to {os.path.join(args.data, core.RESULT_FILE)}")
    return 0


def cmd_status(args):
    summary = core.status_summary(args.data)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def cmd_demo(args):
    lines = core.create_demo_dataset(args.data)
    print(f"demo dataset written to {args.data}:")
    for line in lines:
        print(f"  {line}")
    print("next: python3 app.py plan --data "
          f"{args.data} --now 2026-11-14 && python3 app.py execute "
          f"--data {args.data} --now 2026-11-14")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="Two-step historical chat attachment cleanup (local simulation)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("demo", "create a synthetic demo dataset"),
        ("plan", "step 1: build the operations-confirmed deletion list"),
        ("execute", "step 2: process confirmed entries (resumable)"),
        ("status", "show per-entry journal status"),
    ):
        sub_parser = sub.add_parser(name, help=help_text)
        sub_parser.add_argument("--data", required=True,
                                help="data directory simulating the system")
        if name in ("plan", "execute"):
            sub_parser.add_argument("--now", default=None,
                                    help="ISO date used as 'today' (default: real today)")
    args = parser.parse_args(argv)
    handler = {"demo": cmd_demo, "plan": cmd_plan,
               "execute": cmd_execute, "status": cmd_status}[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
