#!/usr/bin/env python3
"""Local verification entry for the ZIP atomic safe import tool.

Stage 1 (unittest): discovers the suite with
``unittest.defaultTestLoader.discover("tests")``, requires at least one
test case, actually runs it, and requires at least one test to have really
executed (not skipped). Zero discovered tests, all-skipped, any error or
any failure exits non-zero.

Stage 2 (samples): builds synthetic sample/boundary archives in a temp
directory and runs the delivered CLI (app.py) against them, asserting the
expected outcome of each stable case_id. Input, expected and actual results
are printed per case; any mismatch exits non-zero.

Run:  /usr/local/bin/python3 tools/local_verify.py
Exit: 0 = everything passed, 1 = verification failed.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)

import helpers  # noqa: E402  (tests/helpers.py)


# ---------------------------------------------------------------- stage 1

def stage_unittest():
    print("=" * 70)
    print("[stage:unittest] discovering and running tests/")
    print("=" * 70)
    os.chdir(ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    count = suite.countTestCases()
    print("[stage:unittest] discovered %d test case(s)" % count)
    if count < 1:
        print("[stage:unittest] FAIL: zero tests discovered")
        return False
    runner = unittest.TextTestRunner(verbosity=1, stream=sys.stdout)
    result = runner.run(suite)
    skipped = len(result.skipped)
    executed = result.testsRun - skipped
    print("[stage:unittest] testsRun=%d skipped=%d executed=%d failures=%d "
          "errors=%d" % (result.testsRun, skipped, executed,
                         len(result.failures), len(result.errors)))
    if executed < 1:
        print("[stage:unittest] FAIL: no test actually executed "
              "(all skipped or empty)")
        return False
    if not result.wasSuccessful():
        print("[stage:unittest] FAIL: unittest stage had failures/errors")
        return False
    print("[stage:unittest] OK: %d test(s) executed and passed" % executed)
    return True


# ---------------------------------------------------------------- stage 2

CASES = []


def case(case_id, requirement):
    def deco(fn):
        CASES.append((case_id, requirement, fn))
        return fn
    return deco


class Ctx:
    def __init__(self, tmp):
        self.tmp = tmp
        self.inbox = os.path.join(tmp, "inbox")
        self.shared = os.path.join(tmp, "shared")
        os.makedirs(self.inbox)
        os.makedirs(self.shared)

    def zip(self, name, entries, **kw):
        return helpers.write_zip(os.path.join(self.inbox, name), entries, **kw)

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-I", APP] + list(args),
            capture_output=True, text=True, timeout=30)

    def shared_listing(self):
        return sorted(os.listdir(self.shared))


def check(ok, actual, expected):
    return ok, actual, expected


@case("SAMPLE-PLAN-001", "先列出每个条目的目标相对路径")
def sample_plan(ctx):
    zpath = ctx.zip("plan.zip", [("docs/a.txt", b"aa", helpers.MODE_FILE),
                                 ("b.txt", b"bb", helpers.MODE_FILE)])
    proc = ctx.cli("plan", zpath)
    report = json.loads(proc.stdout)
    actual = "exit=%d targets=%s" % (
        proc.returncode, [e["target"] for e in report["entries"]])
    expected = "exit=0 targets=['docs/a.txt', 'b.txt']"
    return check(proc.returncode == 0
                 and [e["target"] for e in report["entries"]] == ["docs/a.txt", "b.txt"],
                 actual, expected)


@case("SAMPLE-IMPORT-001", "以新目录作为一批导入的结果（接收入口：归档+资料目录+限额）")
def sample_import(ctx):
    zpath = ctx.zip("good.zip", [("docs/a.txt", b"alpha", helpers.MODE_FILE),
                                 ("b.txt", b"beta", helpers.MODE_FILE)])
    proc = ctx.cli("import", zpath, ctx.shared,
                   "--max-entry-bytes", "1M", "--max-total-bytes", "10M")
    report = json.loads(proc.stdout)
    batch = report.get("batch_dir", "")
    files_ok = (os.path.isdir(batch)
                and helpers.read_file(batch, "docs/a.txt") == b"alpha"
                and helpers.read_file(batch, "b.txt") == b"beta")
    actual = "exit=%d batch=%s files_ok=%s total=%s" % (
        proc.returncode, report.get("batch_name"), files_ok,
        report.get("total_bytes"))
    expected = "exit=0 batch=<new dir> files_ok=True total=9"
    return check(proc.returncode == 0 and files_ok
                 and report.get("total_bytes") == 9, actual, expected)


@case("SAMPLE-ATOMIC-ESC-001", "路径逃逸条目被拒绝，失败报告定位归档条目，共享区无部分资料")
def sample_escape(ctx):
    zpath = ctx.zip("slip.zip", [("ok.txt", b"fine", helpers.MODE_FILE),
                                 ("../evil.txt", b"pwn", helpers.MODE_FILE)])
    proc = ctx.cli("import", zpath, ctx.shared)
    report = json.loads(proc.stdout)
    err = report.get("error", {})
    escaped = os.path.exists(os.path.join(ctx.tmp, "evil.txt"))
    actual = ("exit=%d code=%s entry=%r index=%s shared=%s escaped=%s"
              % (proc.returncode, err.get("code"), err.get("entry"),
                 err.get("entry_index"), ctx.shared_listing(), escaped))
    expected = ("exit=1 code='path_escape' entry='../evil.txt' index=1 "
                "shared=[] escaped=False")
    ok = (proc.returncode == 1 and err.get("code") == "path_escape"
          and err.get("entry") == "../evil.txt" and err.get("entry_index") == 1
          and ctx.shared_listing() == [] and not escaped)
    return check(ok, actual, expected)


@case("SAMPLE-ATOMIC-CORRUPT-001", "归档读坏时共享区不出现部分资料")
def sample_corrupt(ctx):
    zpath = ctx.zip("broken.zip", [("a.txt", b"x" * 200, helpers.MODE_FILE)])
    with open(zpath, "r+b") as f:
        f.truncate(os.path.getsize(zpath) // 2)
    proc = ctx.cli("import", zpath, ctx.shared)
    report = json.loads(proc.stdout)
    err = report.get("error", {})
    actual = "exit=%d code=%s shared=%s" % (
        proc.returncode, err.get("code"), ctx.shared_listing())
    expected = "exit=1 code='bad_archive' shared=[]"
    ok = (proc.returncode == 1 and err.get("code") == "bad_archive"
          and ctx.shared_listing() == [])
    return check(ok, actual, expected)


@case("SAMPLE-COLL-CASE-001", "按目标文件系统大小写规则识别同一位置的名字")
def sample_case_collision(ctx):
    zpath = ctx.zip("clash.zip", [("Readme.txt", b"one", helpers.MODE_FILE),
                                  ("README.TXT", b"two", helpers.MODE_FILE)])
    proc = ctx.cli("import", zpath, ctx.shared, "--platform", "darwin")
    report = json.loads(proc.stdout)
    err = report.get("error", {})
    actual = "exit=%d code=%s entry=%r shared=%s" % (
        proc.returncode, err.get("code"), err.get("entry"),
        ctx.shared_listing())
    expected = "exit=1 code='name_collision' entry='README.TXT' shared=[]"
    ok = (proc.returncode == 1 and err.get("code") == "name_collision"
          and err.get("entry") == "README.TXT" and ctx.shared_listing() == [])
    return check(ok, actual, expected)


@case("SAMPLE-COLL-UNI-001", "按 Unicode 规范化规则识别同一位置的名字")
def sample_unicode_collision(ctx):
    import unicodedata
    nfc = "café.txt"
    nfd = unicodedata.normalize("NFD", nfc)
    zpath = ctx.zip("uni.zip", [(nfc, b"nfc", helpers.MODE_FILE),
                                (nfd, b"nfd", helpers.MODE_FILE)])
    proc = ctx.cli("import", zpath, ctx.shared, "--platform", "darwin")
    report = json.loads(proc.stdout)
    err = report.get("error", {})
    actual = "exit=%d code=%s entry=%r shared=%s" % (
        proc.returncode, err.get("code"), err.get("entry"),
        ctx.shared_listing())
    expected = "exit=1 code='name_collision' entry=<NFD form> shared=[]"
    ok = (proc.returncode == 1 and err.get("code") == "name_collision"
          and err.get("entry") == nfd and ctx.shared_listing() == [])
    return check(ok, actual, expected)


@case("SAMPLE-LINK-001", "链接条目须拒绝")
def sample_symlink(ctx):
    zpath = ctx.zip("link.zip", [("real.txt", b"data", helpers.MODE_FILE),
                                 ("link.txt", b"/etc/passwd",
                                  helpers.MODE_SYMLINK)])
    proc = ctx.cli("import", zpath, ctx.shared)
    report = json.loads(proc.stdout)
    err = report.get("error", {})
    actual = "exit=%d code=%s entry=%r shared=%s" % (
        proc.returncode, err.get("code"), err.get("entry"),
        ctx.shared_listing())
    expected = "exit=1 code='symlink' entry='link.txt' shared=[]"
    ok = (proc.returncode == 1 and err.get("code") == "symlink"
          and err.get("entry") == "link.txt" and ctx.shared_listing() == [])
    return check(ok, actual, expected)


@case("SAMPLE-LIMIT-ENTRY-001", "实际解压时限制条目字节数")
def sample_entry_limit(ctx):
    zpath = ctx.zip("big-entry.zip", [("b.bin", b"b" * 2048,
                                       helpers.MODE_FILE)])
    proc = ctx.cli("import", zpath, ctx.shared, "--max-entry-bytes", "1K")
    report = json.loads(proc.stdout)
    err = report.get("error", {})
    actual = "exit=%d code=%s entry=%r shared=%s" % (
        proc.returncode, err.get("code"), err.get("entry"),
        ctx.shared_listing())
    expected = "exit=1 code='entry_too_large' entry='b.bin' shared=[]"
    ok = (proc.returncode == 1 and err.get("code") == "entry_too_large"
          and err.get("entry") == "b.bin" and ctx.shared_listing() == [])
    return check(ok, actual, expected)


@case("SAMPLE-LIMIT-TOTAL-001", "实际解压时限制整包展开字节数")
def sample_total_limit(ctx):
    zpath = ctx.zip("big-total.zip", [("a.bin", b"a" * 60, helpers.MODE_FILE),
                                      ("b.bin", b"b" * 60, helpers.MODE_FILE)])
    proc = ctx.cli("import", zpath, ctx.shared,
                   "--max-entry-bytes", "100", "--max-total-bytes", "100")
    report = json.loads(proc.stdout)
    err = report.get("error", {})
    actual = "exit=%d code=%s entry=%r shared=%s" % (
        proc.returncode, err.get("code"), err.get("entry"),
        ctx.shared_listing())
    expected = "exit=1 code='total_too_large' entry='b.bin' shared=[]"
    ok = (proc.returncode == 1 and err.get("code") == "total_too_large"
          and err.get("entry") == "b.bin" and ctx.shared_listing() == [])
    return check(ok, actual, expected)


@case("SAMPLE-BATCH-001", "已存在的批次目录不被覆盖")
def sample_batch_exists(ctx):
    zpath = ctx.zip("one.zip", [("a.txt", b"first", helpers.MODE_FILE)])
    first = ctx.cli("import", zpath, ctx.shared, "--batch-name", "batch-x")
    zpath2 = ctx.zip("two.zip", [("b.txt", b"second", helpers.MODE_FILE)])
    second = ctx.cli("import", zpath2, ctx.shared, "--batch-name", "batch-x")
    report = json.loads(second.stdout)
    err = report.get("error", {})
    kept = helpers.read_file(os.path.join(ctx.shared, "batch-x"), "a.txt")
    actual = ("first_exit=%d second_exit=%d code=%s kept=%r"
              % (first.returncode, second.returncode, err.get("code"), kept))
    expected = "first_exit=0 second_exit=1 code='batch_exists' kept=b'first'"
    ok = (first.returncode == 0 and second.returncode == 1
          and err.get("code") == "batch_exists" and kept == b"first")
    return check(ok, actual, expected)


def stage_samples():
    print("=" * 70)
    print("[stage:samples] running delivered sample/boundary assertions")
    print("=" * 70)
    all_ok = True
    for case_id, requirement, fn in CASES:
        tmp = tempfile.mkdtemp(prefix="verify-sample-")
        try:
            ctx = Ctx(tmp)
            try:
                ok, actual, expected = fn(ctx)
            except Exception as exc:  # noqa: BLE001 - report, don't hide
                ok, actual, expected = False, "exception: %r" % exc, "no exception"
            status = "PASS" if ok else "FAIL"
            all_ok = all_ok and ok
            print("[case %s] %s" % (case_id, status))
            print("  requirement: %s" % requirement)
            print("  expected:    %s" % expected)
            print("  actual:      %s" % actual)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print("[stage:samples] %s" % ("OK: all sample cases passed"
                                  if all_ok else "FAIL: see cases above"))
    return all_ok


def main():
    ok_unit = stage_unittest()
    ok_samples = stage_samples()
    print("=" * 70)
    if ok_unit and ok_samples:
        print("[verify] PASS: unittest stage and sample stage both passed")
        return 0
    print("[verify] FAIL: unittest_ok=%s samples_ok=%s" % (ok_unit, ok_samples))
    return 1


if __name__ == "__main__":
    sys.exit(main())
