#!/usr/bin/env python3
"""本地核验入口(SEC-20261114-021)。

用法: /usr/local/bin/python3 tools/local_verify.py

步骤:
  1. 用 unittest.defaultTestLoader.discover("tests") 发现测试套件,
     要求 countTestCases() >= 1, 随后实际运行;
     实际执行且未跳过的测试数必须 >= 1, 任何失败/错误/异常均判失败。
  2. 运行已交付样例 samples/demo.jsonl, 逐行与 samples/demo_expected.jsonl
     比对, 逐条保留实际结果。
  3. 运行边界断言(窗口终点边界、关闭时刻边界), 逐条保留实际结果。

发现 0 项测试、全部跳过、异常或任何失败均以非零退出。
"""

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def run_unit_tests():
    print("== step 1: unittest discover + run ==")
    os.chdir(ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    count = suite.countTestCases()
    print("[verify] discovered %d test case(s)" % count)
    if count < 1:
        print("[verify] FAIL: discovered 0 test cases")
        return 1
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    skipped = len(result.skipped)
    executed = result.testsRun - skipped
    print("[verify] testsRun=%d skipped=%d executed=%d failures=%d errors=%d"
          % (result.testsRun, skipped, executed,
             len(result.failures), len(result.errors)))
    if executed < 1:
        print("[verify] FAIL: 0 tests actually executed (all skipped?)")
        return 1
    if result.failures or result.errors:
        print("[verify] FAIL: unit tests have failures/errors")
        return 1
    print("[verify] step 1 PASS")
    return 0


def run_sample_assertions():
    print("== step 2: sample demo.jsonl vs demo_expected.jsonl ==")
    demo = os.path.join(ROOT, "samples", "demo.jsonl")
    expected = os.path.join(ROOT, "samples", "demo_expected.jsonl")
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "app.py"), demo],
        capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        print("[verify] FAIL: app.py exited %d: %s"
              % (proc.returncode, proc.stderr.strip()))
        return 1
    actual = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    with open(expected, "r", encoding="utf-8") as fh:
        want = [json.loads(l) for l in fh if l.strip()]
    failures = 0
    if len(want) != len(actual):
        print("[verify] FAIL: line count expected=%d actual=%d"
              % (len(want), len(actual)))
        failures += 1
    for idx, pair in enumerate(zip(want, actual), start=1):
        w, a = pair
        if w == a:
            print("[sample line %02d] PASS action=%s event=%s"
                  % (idx, a.get("action"), a.get("event_id", "-")))
        else:
            failures += 1
            print("[sample line %02d] FAIL" % idx)
            print("  expected: %s" % json.dumps(w, ensure_ascii=False,
                                                sort_keys=True))
            print("  actual  : %s" % json.dumps(a, ensure_ascii=False,
                                                sort_keys=True))
    if failures:
        print("[verify] step 2 FAIL: %d sample assertion(s) failed" % failures)
        return 1
    print("[verify] step 2 PASS: %d sample line(s) matched" % len(want))
    return 0


def run_boundary_assertions():
    print("== step 3: boundary assertions ==")
    from alert_lifecycle import AlertManager

    checks = []

    # B1: 发生时间恰好等于窗口终点(09:10:00) -> 不属于旧窗口, 开启新事件。
    mgr = AlertManager()
    mgr.handle_hit("b1-r1", "acme", "ws-001", "trojan.x",
                   "2026-11-14T09:00:00Z", "2026-11-14T09:00:00Z")
    r = mgr.handle_hit("b1-r2", "acme", "ws-001", "trojan.x",
                       "2026-11-14T09:10:00Z", "2026-11-14T09:10:00Z")
    checks.append(("B1 window_end boundary -> new_event",
                   r["action"] == "new_event" and r["event_id"] == "EVT-0002",
                   "action=%s event_id=%s" % (r["action"], r["event_id"])))

    # B2: 发生时间恰好等于关闭时间 -> 归回旧事件(late_attributed)。
    mgr = AlertManager()
    mgr.handle_hit("b2-r1", "acme", "ws-001", "trojan.x",
                   "2026-11-14T09:00:00Z", "2026-11-14T09:00:00Z")
    mgr.handle_receipt("b2-rc1", "EVT-0001", "confirmed",
                       "2026-11-14T09:06:00Z")
    r = mgr.handle_hit("b2-r2", "acme", "ws-001", "trojan.x",
                       "2026-11-14T09:06:00Z", "2026-11-14T09:07:00Z")
    checks.append(("B2 closed_at boundary -> late_attributed",
                   r["action"] == "late_attributed"
                   and r["event_id"] == "EVT-0001",
                   "action=%s event_id=%s" % (r["action"], r["event_id"])))

    # B3: 窗口内最后一秒(09:09:59)仍聚合到旧事件。
    mgr = AlertManager()
    mgr.handle_hit("b3-r1", "acme", "ws-001", "trojan.x",
                   "2026-11-14T09:00:00Z", "2026-11-14T09:00:00Z")
    r = mgr.handle_hit("b3-r2", "acme", "ws-001", "trojan.x",
                       "2026-11-14T09:09:59Z", "2026-11-14T09:09:59Z")
    checks.append(("B3 window last second -> aggregated",
                   r["action"] == "aggregated" and r["count"] == 2,
                   "action=%s count=%s" % (r["action"], r["count"])))

    # B4: 重复 report_id 不增加计数。
    mgr = AlertManager()
    mgr.handle_hit("b4-r1", "acme", "ws-001", "trojan.x",
                   "2026-11-14T09:00:00Z", "2026-11-14T09:00:00Z")
    r = mgr.handle_hit("b4-r1", "acme", "ws-001", "trojan.x",
                       "2026-11-14T09:00:00Z", "2026-11-14T09:00:01Z")
    checks.append(("B4 duplicate report_id -> count stays 1",
                   r["action"] == "duplicate_ignored" and r["count"] == 1,
                   "action=%s count=%s" % (r["action"], r["count"])))

    failures = 0
    for name, ok, actual in checks:
        status = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print("[boundary %s] %s actual: %s" % (status, name, actual))
    if failures:
        print("[verify] step 3 FAIL: %d boundary assertion(s) failed"
              % failures)
        return 1
    print("[verify] step 3 PASS: %d boundary assertion(s)" % len(checks))
    return 0


def main():
    rc = 0
    rc |= run_unit_tests()
    rc |= run_sample_assertions()
    rc |= run_boundary_assertions()
    if rc == 0:
        print("[verify] ALL CHECKS PASSED")
    else:
        print("[verify] OVERALL FAIL")
    return 1 if rc else 0


if __name__ == "__main__":
    sys.exit(main())
