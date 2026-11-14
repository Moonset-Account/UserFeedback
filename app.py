#!/usr/bin/env python3
"""CLI entry point for the structured event log module.

Subcommands:
  write   Append a single event to a log file.
  batch   Append events from a JSON-lines input file (one event per line);
          malformed lines are reported and skipped without affecting others.
  read    Read a log file back; prints one JSON object per record and points
          out a truncated final record (exit code 3 in that case).
  demo    Run the local sample scenarios (multi-line body, Unicode control
          characters, reserved-field conflict, truncated tail).
"""

from __future__ import annotations

import argparse
import json
import sys

import eventlog


def _parse_field(pair):
    if "=" not in pair:
        raise argparse.ArgumentTypeError("field must be KEY=VALUE, got %r" % pair)
    key, value = pair.split("=", 1)
    return key, value


def cmd_write(args):
    fields = dict(args.field or [])
    try:
        with open(args.file, "ab") as fh:
            eventlog.write_event(fh, args.severity, args.event_id, args.message, fields)
    except eventlog.EventLogError as exc:
        print("rejected: %s" % exc, file=sys.stderr)
        return 1
    print("wrote 1 record to %s" % args.file)
    return 0


def cmd_batch(args):
    events = []
    with open(args.input, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError as exc:
                print("input line %d is not JSON: %s" % (line_no, exc), file=sys.stderr)
                return 1
    with open(args.file, "ab") as fh:
        written, errors = eventlog.write_batch(fh, events)
    for index, exc in errors:
        print("event %d rejected: %s" % (index, exc), file=sys.stderr)
    print("wrote %d/%d records to %s" % (written, len(events), args.file))
    return 0 if not errors else 1


def cmd_read(args):
    try:
        with open(args.file, "rb") as fh:
            events, truncation = eventlog.read_events(fh)
    except eventlog.CorruptLogError as exc:
        print("corrupt log: %s" % exc, file=sys.stderr)
        return 2
    for event in events:
        print(json.dumps(event, ensure_ascii=False, sort_keys=True))
    if truncation is not None:
        print(
            "truncated last record at index {index}: {reason} "
            "(expected {expected} bytes, received {received})".format(**truncation),
            file=sys.stderr,
        )
        return 3
    return 0


def cmd_demo(_args):
    import io

    print("== sample 1: multi-line body round-trip ==")
    body = "line one\nline two\n\nline four with delimiter EVL1 99\\n inside"
    buf = io.BytesIO()
    eventlog.write_event(buf, "ERROR", "EVT-1001", body, {"source": "demo"})
    buf.seek(0)
    events, trunc = eventlog.read_events(buf)
    print("body preserved:", events[0]["message"] == body, "| truncated:", trunc)

    print("== sample 2: Unicode control characters round-trip ==")
    body2 = "nul=\x00 esc=\x1b nel= ls=  tab=\t end"
    buf = io.BytesIO()
    eventlog.write_event(buf, "INFO", "EVT-1002", body2, None)
    buf.seek(0)
    events, trunc = eventlog.read_events(buf)
    print("body preserved:", events[0]["message"] == body2, "| truncated:", trunc)

    print("== sample 3: reserved field conflict is rejected ==")
    try:
        eventlog.write_event(io.BytesIO(), "INFO", "EVT-1003", "x", {"severity": "DEBUG"})
    except eventlog.ReservedFieldError as exc:
        print("rejected as expected, conflicting field:", exc.field)

    print("== sample 4: truncated last record is reported ==")
    buf = io.BytesIO()
    eventlog.write_event(buf, "WARNING", "EVT-1004", "complete", None)
    data = buf.getvalue() + eventlog.build_record("INFO", "EVT-1005", "partial", None)[:10]
    events, trunc = eventlog.read_events(io.BytesIO(data))
    print("complete records:", len(events), "| truncation reported:", trunc)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="app.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("write", help="append a single event")
    p.add_argument("file")
    p.add_argument("--severity", required=True)
    p.add_argument("--event-id", required=True)
    p.add_argument("--message", required=True)
    p.add_argument("--field", action="append", type=_parse_field,
                   help="application field as KEY=VALUE (repeatable)")
    p.set_defaults(func=cmd_write)

    p = sub.add_parser("batch", help="append events from a JSON-lines file")
    p.add_argument("file")
    p.add_argument("input")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("read", help="read events back")
    p.add_argument("file")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("demo", help="run local sample scenarios")
    p.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
