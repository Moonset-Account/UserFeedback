#!/usr/bin/env python3
"""Local verification wrapper for SEC-20261114-030.

1. Discovers the unittest suite via unittest.defaultTestLoader.discover("tests"),
   requires countTestCases() >= 1, actually runs it, and requires at least one
   test that really executed (not skipped). Zero tests, all-skipped, errors or
   failures all exit non-zero.
2. Runs the delivered sample/boundary assertions (CLI on examples/*.json) and
   prints each assertion with its actual vs expected result.

Usage: /usr/local/bin/python3 tools/local_verify.py
"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_unit_tests():
    suite = unittest.defaultTestLoader.discover(
        os.path.join(ROOT, "tests"), top_level_dir=ROOT)
    count = suite.countTestCases()
    print("[unit] discovered %d test case(s)" % count)
    if count < 1:
        print("[unit] FAIL: zero tests discovered")
        return False
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    executed = result.testsRun - len(result.skipped)
    print("[unit] ran=%d skipped=%d executed=%d failures=%d errors=%d"
          % (result.testsRun, len(result.skipped), executed,
             len(result.failures), len(result.errors)))
    if executed < 1:
        print("[unit] FAIL: no test actually executed (all skipped?)")
        return False
    if result.failures or result.errors:
        print("[unit] FAIL: failures or errors present")
        return False
    print("[unit] OK")
    return True


def run_cli(rules_path, tree_path):
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "app.py"), rules_path, tree_path],
        capture_output=True, text=True, timeout=30)
    report = None
    if proc.stdout.strip():
        report = json.loads(proc.stdout)
    return proc.returncode, report, proc.stderr


SAMPLE_ASSERTIONS = []


def sample(case_id, description):
    def deco(fn):
        SAMPLE_ASSERTIONS.append((case_id, description, fn))
        return fn
    return deco


@sample("demo.same-var-binding-passes",
        "demo.json: variable 'user_id' as bound value -> exec q_bind passes")
def s1(report, exit_code):
    by_id = {e["id"]: e for e in report["executions"]}
    actual = by_id["q_bind"]["status"]
    assert actual == "pass", "expected pass, got %r" % actual
    assert by_id["q_bind"]["findings"] == [], "expected no findings"
    return "status=%s findings=0" % actual


@sample("demo.same-var-structure-flagged",
        "demo.json: same variable in query structure -> exec q_struct flagged, "
        "source and location reported")
def s2(report, exit_code):
    by_id = {e["id"]: e for e in report["executions"]}
    ex = by_id["q_struct"]
    assert ex["status"] == "fail", "expected fail, got %r" % ex["status"]
    f = ex["findings"][0]
    assert f["type"] == "tainted_structure", f["type"]
    assert f["source"] == "user_id", f["source"]
    assert f["external_input"] is True, f
    assert ex["location"] == "demo.py:20", ex["location"]
    return "status=fail type=%s source=%s external=%s location=%s" % (
        f["type"], f["source"], f["external_input"], ex["location"])


@sample("demo.exit-code-1-on-risk",
        "demo.json: CLI exit code is 1 when risks exist")
def s3(report, exit_code):
    assert exit_code == 1, "expected exit 1, got %r" % exit_code
    return "exit_code=%d" % exit_code


@sample("cond.risky-branch-reported",
        "cond.json: only the feasible risky branch ('custom') is reported, "
        "allowed constants 'asc'/'desc' pass")
def s4(report, exit_code):
    ex = report["executions"][0]
    assert len(ex["findings"]) == 1, ex["findings"]
    f = ex["findings"][0]
    assert f["branch"] == ["custom"], f["branch"]
    assert f["source"] == "sort_key", f["source"]
    return "findings=1 branch=%s source=%s" % (f["branch"], f["source"])


@sample("mismatch.listed-separately",
        "mismatch.json: 2 param slots vs 1 binding -> separate "
        "binding_mismatches list, no taint findings")
def s5(report, exit_code):
    assert report["summary"]["risk_count"] == 0, report["summary"]
    assert report["summary"]["mismatch_count"] == 1, report["summary"]
    mm = report["binding_mismatches"][0]
    assert mm["id"] == "q_mismatch", mm
    assert mm["param_slots"] == 2 and mm["bindings"] == 1, mm
    return "risk_count=0 mismatch=%s" % (mm,)


def run_samples():
    rules = os.path.join(ROOT, "examples", "rules.json")
    trees = {
        "demo": os.path.join(ROOT, "examples", "demo.json"),
        "cond": os.path.join(ROOT, "examples", "cond.json"),
        "mismatch": os.path.join(ROOT, "examples", "mismatch.json"),
    }
    reports = {}
    exits = {}
    for name, path in trees.items():
        code, report, err = run_cli(rules, path)
        if report is None:
            print("[sample] FAIL: CLI produced no report for %s: %s"
                  % (name, err.strip()))
            return False
        reports[name] = report
        exits[name] = code

    context = {
        "s1": ("demo",), "s2": ("demo",), "s3": ("demo",),
        "s4": ("cond",), "s5": ("mismatch",),
    }
    ok = True
    for case_id, desc, fn in SAMPLE_ASSERTIONS:
        (tree_name,) = context[fn.__name__]
        try:
            actual = fn(reports[tree_name], exits[tree_name])
            print("[sample] PASS %s: %s | actual: %s" % (case_id, desc, actual))
        except AssertionError as exc:
            ok = False
            print("[sample] FAIL %s: %s | %s" % (case_id, desc, exc))
    return ok


def main():
    os.chdir(ROOT)
    ok = run_unit_tests()
    ok = run_samples() and ok
    if not ok:
        print("VERIFY: FAIL")
        return 1
    print("VERIFY: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
