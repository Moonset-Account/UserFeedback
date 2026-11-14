#!/usr/bin/env python3
"""Local verification wrapper for SEC-20261114-059.

Runs two layers of checks and exits non-zero on any problem:

1. The unittest suite discovered via
   ``unittest.defaultTestLoader.discover("tests")``.  The wrapper refuses
   to treat "zero tests" or "all skipped" as success: it requires
   countTestCases() >= 1 and (testsRun - skipped) >= 1, and any failure
   or error makes the run fail.

2. Delivered sample/boundary assertions (case ids S1..S4) that execute
   the real CLI (app.py) on the shipped sample fixtures and check actual
   outputs against independently recomputed expectations.  Each case
   prints its actual vs expected result.

Usage:  /usr/local/bin/python3 tools/local_verify.py
"""

import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

KEYS = {
    ("tenant-a", "v1"): "aa10" * 16,
    ("tenant-a", "v2"): "bb20" * 16,
    ("tenant-b", "v1"): "cc30" * 16,
}


def expected_codename(tenant, key_version, purpose, value):
    """Independent HMAC recomputation (does not import transform.py)."""
    key_bytes = bytes.fromhex(KEYS[(tenant, key_version)])
    msg = "\x00".join([tenant, key_version, purpose, value]).encode("utf-8")
    return "cod_" + hmac.new(key_bytes, msg, hashlib.sha256).hexdigest()[:24]


def run_unittest_suite():
    """Discover and run the unittest suite; return (ok, detail)."""
    os.chdir(ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    count = suite.countTestCases()
    if count < 1:
        return False, "discovered 0 test cases"
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    executed = result.testsRun - len(result.skipped)
    if executed < 1:
        return False, "0 tests actually executed (all skipped?)"
    if result.failures or result.errors:
        return False, "%d failures, %d errors out of %d executed" % (
            len(result.failures), len(result.errors), executed)
    return True, "%d tests executed, 0 failures, 0 errors, %d skipped" % (
        executed, len(result.skipped))


def run_cli(source, task, inp, out, err):
    cmd = [sys.executable, os.path.join(ROOT, "app.py"),
           "--source", source,
           "--task", task,
           "--input", inp,
           "--output", out,
           "--errors", err,
           "--keystore", os.path.join(ROOT, "keys.json"),
           "--policies-dir", os.path.join(ROOT, "policies")]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          timeout=30)
    return proc


def read_jsonl(path):
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class CaseResult:
    def __init__(self, case_id, description):
        self.case_id = case_id
        self.description = description
        self.checks = []  # (name, ok, actual, expected)

    def check(self, name, actual, expected):
        self.checks.append((name, actual == expected, actual, expected))

    def check_true(self, name, actual):
        self.checks.append((name, bool(actual), actual, True))

    @property
    def ok(self):
        return all(ok for _n, ok, _a, _e in self.checks) and bool(self.checks)

    def render(self):
        lines = ["CASE %s: %s -> %s" % (
            self.case_id, self.description, "PASS" if self.ok else "FAIL")]
        for name, ok, actual, expected in self.checks:
            lines.append("  [%s] %s: actual=%r expected=%r" % (
                "PASS" if ok else "FAIL", name, actual, expected))
        return "\n".join(lines)


def sample_cases():
    results = []
    tmp = tempfile.mkdtemp(prefix="sec059-verify-")

    # ---- S1: gateway sample conversion, whitelist, skip+error report ----
    r = CaseResult("S1", "gateway sample: whitelist output, skipped lines, "
                         "error report shape")
    out, err = os.path.join(tmp, "gw.out"), os.path.join(tmp, "gw.err")
    proc = run_cli("gateway", os.path.join(ROOT, "samples", "task.json"),
                   os.path.join(ROOT, "samples", "gateway.jsonl"), out, err)
    r.check("exit code", proc.returncode, 0)
    if proc.returncode == 0:
        recs = read_jsonl(out)
        errs = read_jsonl(err)
        r.check("output record count (lines 1-2 valid)", len(recs), 2)
        if recs:
            r.check("alice codename (independent HMAC)",
                    recs[0]["user"]["name"],
                    expected_codename("tenant-a", "v1", "account", "alice"))
            r.check("bob codename (independent HMAC)",
                    recs[1]["user"]["name"],
                    expected_codename("tenant-a", "v1", "account", "bob"))
            r.check("record 1 top-level keys only from policy",
                    sorted(recs[0]),
                    ["action", "dst", "http", "src", "ts", "user"])
            r.check_true("undeclared session_token dropped",
                         "session_token" not in recs[0])
            r.check("nested src keeps only declared ip",
                    recs[0]["src"], {"ip": "10.0.0.5"})
        r.check("error entry count (lines 3,4,5)", len(errs), 3)
        if len(errs) == 3:
            r.check("error entries keep only source/line/reason",
                    [sorted(e) for e in errs],
                    [["line", "reason", "source"]] * 3)
            r.check("error lines", [e["line"] for e in errs], [3, 4, 5])
            r.check("error reasons",
                    [e["reason"] for e in errs],
                    ["missing required field: user.name",
                     "non-string value at replace path: user.name",
                     "invalid JSON"])
            r.check("error source", [e["source"] for e in errs],
                    ["gateway"] * 3)
    results.append(r)

    # ---- S2: endpoint sample: arrays + cross-source codename equality ----
    r = CaseResult("S2", "endpoint sample: array elements handled, "
                         "cross-source same-account codename")
    out2, err2 = os.path.join(tmp, "ep.out"), os.path.join(tmp, "ep.err")
    proc = run_cli("endpoint", os.path.join(ROOT, "samples", "task.json"),
                   os.path.join(ROOT, "samples", "endpoint.jsonl"),
                   out2, err2)
    r.check("exit code", proc.returncode, 0)
    if proc.returncode == 0:
        recs = read_jsonl(out2)
        errs = read_jsonl(err2)
        r.check("output record count (line 3 skipped)", len(recs), 2)
        r.check("error count", len(errs), 1)
        if errs:
            r.check("error entry", errs[0],
                    {"source": "endpoint", "line": 3,
                     "reason": "missing required field: ts"})
        if recs:
            first = recs[0]
            gw_alice = expected_codename("tenant-a", "v1", "account", "alice")
            r.check("identity.user codename matches gateway alice",
                    first["identity"]["user"], gw_alice)
            r.check("alerts[0] keys (debug dropped)",
                    sorted(first["alerts"][0]), ["owner", "rule", "severity"])
            r.check("alerts[0].owner == identity.user (same account)",
                    first["alerts"][0]["owner"], first["identity"]["user"])
            r.check("alerts[1].owner codename (carol)",
                    first["alerts"][1]["owner"],
                    expected_codename("tenant-a", "v1", "account", "carol"))
            r.check("empty alerts array preserved", recs[1]["alerts"], [])
    results.append(r)

    # ---- S3: determinism: identical rerun -> identical bytes ----
    r = CaseResult("S3", "determinism: same input rerun gives identical "
                         "record order and codenames")
    outs = []
    for i in (1, 2):
        o, e = os.path.join(tmp, "det%d.out" % i), os.path.join(tmp, "det%d.err" % i)
        proc = run_cli("gateway", os.path.join(ROOT, "samples", "task.json"),
                       os.path.join(ROOT, "samples", "gateway.jsonl"), o, e)
        r.check("run %d exit code" % i, proc.returncode, 0)
        with open(o, "rb") as fh:
            outs.append((fh.read(), open(e, "rb").read()))
    if len(outs) == 2:
        r.check_true("output bytes identical across reruns",
                     outs[0][0] == outs[1][0])
        r.check_true("error bytes identical across reruns",
                     outs[0][1] == outs[1][1])
    results.append(r)

    # ---- S4: tenant change -> codename change ----
    r = CaseResult("S4", "context change: different tenant yields different "
                         "codename for the same account")
    out4, err4 = os.path.join(tmp, "tb.out"), os.path.join(tmp, "tb.err")
    proc = run_cli("gateway", os.path.join(ROOT, "samples", "task-b.json"),
                   os.path.join(ROOT, "samples", "gateway.jsonl"), out4, err4)
    r.check("exit code", proc.returncode, 0)
    if proc.returncode == 0:
        recs = read_jsonl(out4)
        if recs:
            tb_alice = recs[0]["user"]["name"]
            r.check("tenant-b alice codename (independent HMAC)",
                    tb_alice,
                    expected_codename("tenant-b", "v1", "account", "alice"))
            r.check_true("tenant-b codename != tenant-a codename",
                         tb_alice != expected_codename(
                             "tenant-a", "v1", "account", "alice"))
    results.append(r)

    return results


def main():
    print("=== layer 1: unittest suite ===")
    suite_ok, suite_detail = run_unittest_suite()
    print("unittest suite: %s (%s)" % ("PASS" if suite_ok else "FAIL",
                                       suite_detail))

    print("\n=== layer 2: sample/boundary assertions ===")
    results = sample_cases()
    for r in results:
        print(r.render())
        print()

    cases_ok = all(r.ok for r in results)
    print("summary: unittest=%s sample_cases=%d/%d passed" % (
        "PASS" if suite_ok else "FAIL",
        sum(1 for r in results if r.ok), len(results)))
    if not (suite_ok and cases_ok):
        print("VERIFY: FAIL")
        return 1
    print("VERIFY: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
