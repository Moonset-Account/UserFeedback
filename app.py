#!/usr/bin/env python3
"""CLI entry point for the local quarantine state module (SEC-20261114-018).

Reads a JSON command batch from a file argument (or stdin when the argument
is omitted or ``-``), applies the commands to a fresh QuarantineStore, and
prints a JSON report to stdout.

Input format (either a bare list, or an object with a "commands" list):

    {"commands": [
        {"op": "record_delivery", "email": "m1", "recipient": "alice",
         "version": "v1", "reasons": ["spam"]},
        {"op": "submit_approval", "email": "m1", "recipient": "alice",
         "approval_id": "ap-1", "version": "v1", "reasons": ["spam"]},
        {"op": "release", "email": "m1", "recipient": "alice",
         "request_id": "req-1"},
        {"op": "confirm_downstream", "email": "m1", "recipient": "alice",
         "confirmation_id": "cfm:m1:alice:v1"},
        {"op": "status", "email": "m1"}
    ]}

Output: {"results": [{"index", "op", "ok", "result"|"error"}, ...]}.
Per-command ``ok`` flags carry business-level failures (e.g. a stale
approval); the process exit code is 0 whenever the batch itself was parsed
and executed, and 2 only for malformed input / usage errors.
"""

import json
import sys

from quarantine import QuarantineError, QuarantineStore


def run_commands(commands, store=None):
    """Apply a command list to ``store`` and return per-command results."""
    if store is None:
        store = QuarantineStore()
    results = []
    for index, command in enumerate(commands):
        op = command.get("op") if isinstance(command, dict) else None
        try:
            if op == "record_delivery":
                outcome = store.record_delivery(
                    command["email"],
                    command["recipient"],
                    command["version"],
                    command["reasons"],
                )
            elif op == "submit_approval":
                outcome = store.submit_approval(
                    command["email"],
                    command["recipient"],
                    command["approval_id"],
                    command["version"],
                    command["reasons"],
                )
            elif op == "release":
                outcome = store.release(
                    command["email"],
                    command["recipient"],
                    command["request_id"],
                )
            elif op == "confirm_downstream":
                outcome = store.confirm_downstream(
                    command["email"],
                    command["recipient"],
                    command["confirmation_id"],
                )
            elif op == "status":
                outcome = store.mail_status(command["email"])
            else:
                raise QuarantineError("unknown op: %r" % (op,))
            results.append(
                {"index": index, "op": op, "ok": True, "result": outcome}
            )
        except QuarantineError as exc:
            results.append(
                {"index": index, "op": op, "ok": False, "error": str(exc)}
            )
        except (KeyError, TypeError) as exc:
            results.append(
                {
                    "index": index,
                    "op": op,
                    "ok": False,
                    "error": "malformed command: %r" % (exc,),
                }
            )
    return results


def main(argv):
    if len(argv) > 2:
        print("usage: app.py [input.json|-]", file=sys.stderr)
        return 2
    if len(argv) == 2 and argv[1] != "-":
        with open(argv[1], "r", encoding="utf-8") as handle:
            text = handle.read()
    else:
        text = sys.stdin.read()
    try:
        payload = json.loads(text)
    except ValueError as exc:
        print("invalid JSON input: %s" % exc, file=sys.stderr)
        return 2
    commands = payload.get("commands") if isinstance(payload, dict) else payload
    if not isinstance(commands, list):
        print(
            "input must be a JSON list of commands or an object with a "
            "'commands' list",
            file=sys.stderr,
        )
        return 2
    results = run_commands(commands)
    json.dump({"results": results}, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
