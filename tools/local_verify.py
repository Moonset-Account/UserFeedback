#!/usr/bin/env python3
"""Local verification wrapper for the structured event log deliverable.

Runs two layers of checks and exits non-zero on any problem:

1. The unittest suite discovered via unittest.defaultTestLoader.discover("tests").
   Zero discovered tests, all-skipped tests, errors or failures all fail the
   run (a zero-test unittest exit-0 is never treated as a pass).
2. The delivered sample/boundary assertions, one per requirement, each
   printing case_id / input / expected / actual / result.
"""

import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import eventlog  # noqa: E402


def run_unittest_suite():
    suite = unittest.defaultTestLoader.discover("tests")
    total = suite.countTestCases()
    print("[unittest] discovered %d test case(s)" % total)
    if total < 1:
        print("[unittest] FAIL: zero tests discovered")
        return False
    runner = unittest.TextTestRunner(verbosity=1, stream=sys.stdout)
    result = runner.run(suite)
    executed = result.testsRun - len(result.skipped)
    print("[unittest] ran=%d skipped=%d executed=%d failures=%d errors=%d"
          % (result.testsRun, len(result.skipped), executed,
             len(result.failures), len(result.errors)))
    if executed < 1:
        print("[unittest] FAIL: no test actually executed (all skipped?)")
        return False
    if result.failures or result.errors:
        print("[unittest] FAIL: failures or errors present")
        return False
    print("[unittest] PASS")
    return True


def _roundtrip(message, fields=None):
    buf = io.BytesIO()
    eventlog.write_event(buf, "ERROR", "EVT-SAMPLE", message, fields)
    buf.seek(0)
    events, truncation = eventlog.read_events(buf)
    return events, truncation


def run_sample_checks():
    """Delivered sample/boundary assertions, one per requirement."""
    checks = []

    # CASE-01: multi-line body round-trips byte-exactly.
    body = "line one\nline two\r\nline three\n\nline five"
    events, trunc = _roundtrip(body)
    checks.append({
        "case_id": "CASE-01-multiline-body",
        "input": repr(body),
        "expected": repr(body),
        "actual": repr(events[0]["message"]) if events else "<no record>",
        "ok": len(events) == 1 and events[0]["message"] == body and trunc is None,
    })

    # CASE-02: Unicode control characters round-trip byte-exactly.
    body = "nul=\x00 esc=\x1b nel=\x85 ls=  ps=  del=\x7f"
    events, trunc = _roundtrip(body)
    checks.append({
        "case_id": "CASE-02-unicode-control-chars",
        "input": repr(body),
        "expected": repr(body),
        "actual": repr(events[0]["message"]) if events else "<no record>",
        "ok": len(events) == 1 and events[0]["message"] == body and trunc is None,
    })

    # CASE-03: delimiter-like bytes inside the body cannot reshape framing.
    body = "EVL1 5\nEVL1 0\n"
    events, trunc = _roundtrip(body)
    checks.append({
        "case_id": "CASE-03-delimiter-in-body",
        "input": repr(body),
        "expected": repr(body),
        "actual": repr(events[0]["message"]) if events else "<no record>",
        "ok": len(events) == 1 and events[0]["message"] == body and trunc is None,
    })

    # CASE-04: reserved field conflict is rejected and names the field.
    field_name = None
    try:
        eventlog.build_record("INFO", "EVT-SAMPLE", "x", {"severity": "DEBUG"})
    except eventlog.ReservedFieldError as exc:
        field_name = exc.field
    checks.append({
        "case_id": "CASE-04-reserved-field-conflict",
        "input": "fields={'severity': 'DEBUG'}",
        "expected": "ReservedFieldError naming 'severity'",
        "actual": "ReservedFieldError naming %r" % (field_name,),
        "ok": field_name == "severity",
    })

    # CASE-05: one malformed record in a batch does not change the trusted
    # metadata of the other events.
    batch = [
        {"severity": "INFO", "event_id": "EVT-A", "message": "good-1"},
        {"severity": "BAD", "event_id": "EVT-B", "message": "bad severity"},
        {"severity": "CRITICAL", "event_id": "EVT-C", "message": "good-2"},
    ]
    buf = io.BytesIO()
    written, errors = eventlog.write_batch(buf, batch)
    buf.seek(0)
    events, trunc = eventlog.read_events(buf)
    meta = [(e["severity"], e["event_id"], e["message"]) for e in events]
    expected_meta = [("INFO", "EVT-A", "good-1"), ("CRITICAL", "EVT-C", "good-2")]
    checks.append({
        "case_id": "CASE-05-batch-isolation",
        "input": "3 events, middle one has invalid severity 'BAD'",
        "expected": repr(expected_meta),
        "actual": repr(meta),
        "ok": (written, len(errors), meta, trunc) == (2, 1, expected_meta, None),
    })

    # CASE-06: read entry points out a truncated last record.
    buf = io.BytesIO()
    eventlog.write_event(buf, "WARNING", "EVT-T1", "complete")
    tail = eventlog.build_record("INFO", "EVT-T2", "partial")
    buf.write(tail[: len(tail) - 5])  # cut the final record mid-payload
    buf.seek(0)
    events, trunc = eventlog.read_events(buf)
    checks.append({
        "case_id": "CASE-06-truncated-last-record",
        "input": "1 complete record + final record missing its last 5 bytes",
        "expected": "1 event + truncation at index 1",
        "actual": "%d event(s) + truncation=%r" % (len(events), trunc),
        "ok": len(events) == 1 and trunc is not None and trunc["index"] == 1,
    })

    all_ok = True
    for check in checks:
        result = "PASS" if check["ok"] else "FAIL"
        all_ok = all_ok and check["ok"]
        print("[sample] %s %s" % (check["case_id"], result))
        print("    input:    %s" % check["input"])
        print("    expected: %s" % check["expected"])
        print("    actual:   %s" % check["actual"])
    return all_ok


def main():
    ok_suite = run_unittest_suite()
    ok_samples = run_sample_checks()
    if ok_suite and ok_samples:
        print("LOCAL VERIFY: PASS")
        return 0
    print("LOCAL VERIFY: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
