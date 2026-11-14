#!/usr/bin/env python3
"""本地核验入口(仅标准库)。

两步核验,任一步失败即非零退出:
  1. 用 unittest.defaultTestLoader.discover("tests") 发现并运行测试套件;
     发现 0 项、全部跳过、或存在失败/错误,均判失败。
  2. 运行交付的样例(samples/)与边界断言:实际执行 app.py 重建台账,
     逐条比对预期并打印实际结果。

用法: /usr/local/bin/python3 tools/local_verify.py
"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_DIR = os.path.join(ROOT, "build")
SAMPLES_DIR = os.path.join(ROOT, "samples")


# ---------------------------------------------------------------- 单元测试

def run_unit_tests():
    os.chdir(ROOT)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    total = suite.countTestCases()
    print("[verify] 发现测试用例数: %d" % total)
    if total < 1:
        print("[verify] 失败: 未发现任何测试用例")
        return False
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    skipped = len(result.skipped)
    executed = result.testsRun - skipped
    print("[verify] testsRun=%d skipped=%d executed=%d failures=%d errors=%d"
          % (result.testsRun, skipped, executed,
             len(result.failures), len(result.errors)))
    if executed < 1:
        print("[verify] 失败: 没有实际执行(未跳过)的测试")
        return False
    if result.failures or result.errors:
        print("[verify] 失败: 存在测试失败或错误")
        return False
    print("[verify] 单元测试全部通过")
    return True


# ---------------------------------------------------------------- 样例断言

def _ev(report, eid):
    for item in report["evidence"]:
        if item["evidence_id"] == eid:
            return item
    raise KeyError("evidence %s 不在报告中" % eid)


def _run_app_on_samples():
    os.makedirs(BUILD_DIR, exist_ok=True)
    out_path = os.path.join(BUILD_DIR, "sample_report.json")
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "app.py"),
         "--input-dir", SAMPLES_DIR, "--output", out_path, "--pretty"],
        capture_output=True, text=True, cwd=ROOT)
    if proc.returncode != 0:
        print("[verify] app.py 运行失败: %s%s" % (proc.stdout, proc.stderr))
        return None
    with open(out_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def sample_assertions(report):
    """返回 [(case_id, 描述, 实际值, 期望值), ...]"""
    A = []

    def add(case_id, desc, actual, expected):
        A.append((case_id, desc, actual, expected))

    # EV-001: 借出未确认 —— 台账应显示交接中,保管人不变
    add("SMP-EV001-STATUS", "EV-001 借出未确认,状态为交接中",
        _ev(report, "EV-001")["status"], "in_transfer")
    add("SMP-EV001-CUSTODIAN", "EV-001 保管人仍为 alice",
        _ev(report, "EV-001")["current_custodian"], "alice")
    add("SMP-EV001-PENDING", "EV-001 待确认移交为 T-100(alice->bob)",
        [(t["transfer_id"], t["from"], t["to"])
         for t in _ev(report, "EV-001")["pending_transfers"]],
        [("T-100", "alice", "bob")])
    add("SMP-EV001-CONFIRMED-THROUGH", "EV-001 连续核对到 seq 3",
        _ev(report, "EV-001")["confirmed_through_seq"], 3)

    # EV-002: 完整交接;跨批次重复确认只应用一次
    add("SMP-EV002-CUSTODIAN", "EV-002 确认后保管人变为 bob",
        _ev(report, "EV-002")["current_custodian"], "bob")
    add("SMP-EV002-STATUS", "EV-002 状态为在库保管",
        _ev(report, "EV-002")["status"], "in_custody")
    confirms = [a for a in _ev(report, "EV-002")["applied_events"]
                if a["type"] == "receive_confirm"]
    add("SMP-EV002-DUP-APPLIED-ONCE", "EV-002 重复确认事件只应用一次",
        len(confirms), 1)
    add("SMP-EV002-DUP-LOCATIONS", "EV-002 重复事件的两个来源位置都保留",
        len(confirms[0]["locations"]) if confirms else 0, 2)

    # EV-003: seq 3 缺失 —— 只确认到 seq 2,后续留人工复核
    add("SMP-EV003-CONFIRMED-THROUGH", "EV-003 断档,只确认到 seq 2",
        _ev(report, "EV-003")["confirmed_through_seq"], 2)
    add("SMP-EV003-INTERRUPT-REASON", "EV-003 中断原因为序号断档",
        _ev(report, "EV-003")["interruption"]["reason"], "sequence_gap")
    add("SMP-EV003-NEEDS-REVIEW", "EV-003 seq 4/5 留待人工复核",
        [r["seq"] for r in _ev(report, "EV-003")["needs_review"]], [4, 5])
    add("SMP-EV003-REVIEW-LOCATIONS",
        "EV-003 待复核记录均带原始文件与行号",
        all("file" in loc and "line" in loc
            for r in _ev(report, "EV-003")["needs_review"]
            for loc in r["locations"]), True)
    add("SMP-EV003-CUSTODIAN", "EV-003 断档后确认未应用,保管人仍为 alice",
        _ev(report, "EV-003")["current_custodian"], "alice")
    add("SMP-EV003-STATUS", "EV-003 移交仍待确认,状态为交接中",
        _ev(report, "EV-003")["status"], "in_transfer")

    # EV-004: seq 2 两种内容 —— 都保留并标明冲突,只确认到 seq 1
    add("SMP-EV004-CONFLICT-SEQS", "EV-004 冲突标在 seq 2",
        [c["seq"] for c in _ev(report, "EV-004")["conflicts"]], [2])
    add("SMP-EV004-CONFLICT-VARIANTS", "EV-004 两种冲突内容都保留",
        len(_ev(report, "EV-004")["conflicts"][0]["variants"]), 2)
    add("SMP-EV004-CONFIRMED-THROUGH", "EV-004 冲突即中断,只确认到 seq 1",
        _ev(report, "EV-004")["confirmed_through_seq"], 1)
    add("SMP-EV004-CUSTODIAN", "EV-004 保管人仍为 alice",
        _ev(report, "EV-004")["current_custodian"], "alice")

    # EV-005: 非保管人发起移交 —— 拒绝,后续复核事件仍正常入账
    add("SMP-EV005-REJECTED", "EV-005 非保管人发起的移交被拒绝",
        [(r["seq"], r["reason"]) for r in _ev(report, "EV-005")["rejected_events"]],
        [(2, "initiator_not_current_custodian")])
    add("SMP-EV005-CUSTODIAN", "EV-005 保管人仍为 alice",
        _ev(report, "EV-005")["current_custodian"], "alice")
    add("SMP-EV005-CONFIRMED-THROUGH", "EV-005 连续核对到 seq 3",
        _ev(report, "EV-005")["confirmed_through_seq"], 3)

    # EV-006: 移交被发起人撤销 —— 无待确认移交
    add("SMP-EV006-PENDING", "EV-006 撤销后无待确认移交",
        _ev(report, "EV-006")["pending_transfers"], [])
    add("SMP-EV006-CUSTODIAN", "EV-006 保管人仍为 alice",
        _ev(report, "EV-006")["current_custodian"], "alice")

    # 全局: 样例文件不应产生无法入档的记录
    add("SMP-INVALID-RECORDS", "样例输入全部可入档",
        report["invalid_records"], [])
    return A


def run_sample_assertions():
    print("[verify] 运行样例/边界断言 (app.py --input-dir samples/)")
    report = _run_app_on_samples()
    if report is None:
        return False
    failures = 0
    for case_id, desc, actual, expected in sample_assertions(report):
        ok = actual == expected
        if not ok:
            failures += 1
        print("[assert] %s %s actual=%r expected=%r -- %s"
              % (case_id, "PASS" if ok else "FAIL", actual, expected, desc))
    total = len(sample_assertions(report))
    print("[verify] 样例断言: %d 项, 失败 %d 项" % (total, failures))
    return failures == 0


# ---------------------------------------------------------------- 入口

def main():
    ok_tests = run_unit_tests()
    ok_samples = run_sample_assertions()
    if ok_tests and ok_samples:
        print("[verify] 全部核验通过")
        return 0
    print("[verify] 核验未通过 (unit_tests=%s, sample_assertions=%s)"
          % ("PASS" if ok_tests else "FAIL",
             "PASS" if ok_samples else "FAIL"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
