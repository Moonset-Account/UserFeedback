#!/usr/bin/env python3
"""Local verification wrapper for SEC-20261114-014.

Runs the unittest suite discovered from ``tests/`` and then drives the
delivered CLI (``app.py``) through the sample/boundary scenario, asserting
each expected outcome and printing the actual result of every check.

Exit code is non-zero when:
  * zero tests are discovered,
  * zero tests actually execute (all skipped / loader error),
  * any test fails or errors,
  * any sample/boundary assertion fails.

Run with:  /usr/local/bin/python3 tools/local_verify.py
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_unit_suite():
    os.chdir(ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    discovered = suite.countTestCases()
    print("[verify] discovered {} test case(s)".format(discovered))
    if discovered < 1:
        print("[verify] FAIL: zero tests discovered in tests/")
        return False
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)
    executed = result.testsRun - len(result.skipped)
    print("[verify] executed {} non-skipped test(s), {} skipped".format(
        executed, len(result.skipped)))
    if executed < 1:
        print("[verify] FAIL: no test actually executed")
        return False
    if not result.wasSuccessful():
        print("[verify] FAIL: unit suite had failures/errors")
        return False
    print("[verify] unit suite OK")
    return True


def run_cli(state_dir, *cli_args):
    cmd = [sys.executable, os.path.join(ROOT, "app.py"),
           "--state-dir", state_dir] + list(cli_args)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError("CLI failed: {}\n{}".format(
            " ".join(cmd), proc.stderr.strip()))
    return json.loads(proc.stdout)


def check(results, case_id, description, actual, expected):
    ok = actual == expected
    results.append(ok)
    status = "PASS" if ok else "FAIL"
    print("[verify] {} {}: {}".format(status, case_id, description))
    print("         actual   = {!r}".format(actual))
    print("         expected = {!r}".format(expected))


def run_sample_assertions():
    """Drive the delivered CLI through the end-to-end sample scenario."""
    results = []
    with tempfile.TemporaryDirectory() as state:
        # SAMPLE-01: version-gated open records original value + written version.
        run_cli(state, "init-device", "--device", "dev-9", "--version", "v9",
                "--probe", "temp=20")
        opened = run_cli(state, "open", "--device", "dev-9", "--probe", "temp",
                         "--value", "ro-diag", "--work-order", "WO-900",
                         "--approved-version", "v9",
                         "--start", "2026-11-14T09:00:00",
                         "--end", "2026-11-14T10:00:00",
                         "--now", "2026-11-14T09:00:00")
        check(results, "SAMPLE-01a", "open with matching approved version accepted",
              opened["accepted"], True)
        window_id = opened["window"]["window_id"]
        check(results, "SAMPLE-01b", "original value recorded on the window",
              opened["window"]["original_value"], "20")
        check(results, "SAMPLE-01c", "written version token recorded",
              opened["window"]["written_version"], "managed-" + window_id)
        check(results, "SAMPLE-01d", "window reached OPEN after device ack",
              opened["window"]["state"], "OPEN")

        # SAMPLE-02: stale approved version is rejected without side effects.
        rejected = run_cli(state, "open", "--device", "dev-9", "--probe", "temp",
                           "--value", "x", "--work-order", "WO-901",
                           "--approved-version", "v8",
                           "--start", "2026-11-14T09:00:00",
                           "--end", "2026-11-14T10:00:00",
                           "--now", "2026-11-14T09:00:00")
        check(results, "SAMPLE-02a", "stale approved version rejected",
              (rejected["accepted"], rejected["reason"]),
              (False, "version-mismatch"))

        # SAMPLE-03 (boundary): one second before end the window stays open.
        early = run_cli(state, "tick", "--now", "2026-11-14T09:59:59")
        check(results, "SAMPLE-03", "tick before end performs no action",
              early["actions"], [])

        # SAMPLE-04 (boundary): exactly at end the window expires and restores.
        due = run_cli(state, "tick", "--now", "2026-11-14T10:00:00")
        check(results, "SAMPLE-04a", "tick at end restores the window",
              [(a["window_id"], a["action"]) for a in due["actions"]],
              [(window_id, "restored")])
        status = run_cli(state, "status")
        check(results, "SAMPLE-04b", "probe value restored to original",
              status["devices"]["dev-9"]["probes"]["temp"], "20")
        check(results, "SAMPLE-04c", "window closed as restored",
              status["windows"][window_id]["state"], "CLOSED_RESTORED")

        # SAMPLE-05: duplicate restore receipt is a no-op.
        dup = run_cli(state, "receipt",
                      "--receipt-id", "rcpt-{}-restore".format(window_id))
        check(results, "SAMPLE-05", "duplicate receipt causes no new migration",
              (dup["applied"], dup["duplicate"]), (False, True))

        # SAMPLE-06: human change during a later window escalates at expiry.
        run_cli(state, "open", "--device", "dev-9", "--probe", "temp",
                "--value", "ro-diag", "--work-order", "WO-902",
                "--approved-version", "v9",
                "--start", "2026-11-14T11:00:00",
                "--end", "2026-11-14T12:00:00",
                "--now", "2026-11-14T11:00:00")
        run_cli(state, "human-edit", "--device", "dev-9",
                "--probe", "temp", "--value", "99")
        esc = run_cli(state, "tick", "--now", "2026-11-14T12:00:00")
        check(results, "SAMPLE-06a", "human change escalates instead of restoring",
              esc["actions"][0]["action"], "escalated")
        status = run_cli(state, "status")
        check(results, "SAMPLE-06b", "human value preserved for manual handling",
              status["devices"]["dev-9"]["probes"]["temp"], "99")

        # SAMPLE-07: offline device defers; reconnect drains expired windows.
        run_cli(state, "init-device", "--device", "dev-10", "--version", "v3",
                "--probe", "temp=5")
        run_cli(state, "open", "--device", "dev-10", "--probe", "temp",
                "--value", "ro", "--work-order", "WO-903",
                "--approved-version", "v3",
                "--start", "2026-11-14T13:00:00",
                "--end", "2026-11-14T14:00:00",
                "--now", "2026-11-14T13:00:00")
        run_cli(state, "offline", "--device", "dev-10")
        deferred = run_cli(state, "tick", "--now", "2026-11-14T14:30:00")
        check(results, "SAMPLE-07a", "expiry on offline device is deferred",
              deferred["actions"][0]["action"], "deferred-offline")
        drained = run_cli(state, "reconnect", "--device", "dev-10",
                          "--now", "2026-11-14T14:30:00")
        check(results, "SAMPLE-07b", "reconnect restores the expired window",
              drained["actions"][0]["action"], "restored")
        status = run_cli(state, "status")
        check(results, "SAMPLE-07c", "probe restored after reconnect",
              status["devices"]["dev-10"]["probes"]["temp"], "5")

    failed = results.count(False)
    print("[verify] sample/boundary assertions: {} passed, {} failed".format(
        results.count(True), failed))
    return failed == 0


def main():
    ok = run_unit_suite()
    if ok:
        ok = run_sample_assertions()
    if not ok:
        print("[verify] OVERALL: FAIL")
        return 1
    print("[verify] OVERALL: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
