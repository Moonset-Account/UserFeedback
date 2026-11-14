#!/usr/bin/env python3
"""本地核验入口：运行全部 unittest，再逐条执行本题样例/边界断言。

用法：/usr/local/bin/python3 tools/local_verify.py
退出码：0 = 全部通过；非 0 = 发现 0 项测试、全部跳过、测试失败或样例断言失败。
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def run_unit_tests():
    suite = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests"))
    count = suite.countTestCases()
    print("[verify] discovered %d test case(s)" % count)
    if count < 1:
        print("[verify] FAIL: 未发现任何测试")
        return False
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    executed = result.testsRun - len(result.skipped)
    print("[verify] testsRun=%d skipped=%d executed=%d"
          % (result.testsRun, len(result.skipped), executed))
    if executed < 1:
        print("[verify] FAIL: 实际执行且未跳过的测试数为 0")
        return False
    if not result.wasSuccessful():
        print("[verify] FAIL: 存在失败或错误的测试")
        return False
    return True


def run_sample_assertions():
    """逐条执行两班连续样例断言，打印 case_id / 期望 / 实际 / 结论。"""
    import app
    import store

    checks = []

    def add(case_id, desc, actual, expected):
        checks.append({
            "case_id": case_id,
            "desc": desc,
            "expected": expected,
            "actual": actual,
            "ok": actual == expected,
        })

    with tempfile.TemporaryDirectory() as d:
        state_path = os.path.join(d, "state.json")
        results = {}
        for name in ("shift_s1.json", "shift_s2.json"):
            spec = app.load_shift_spec(os.path.join(ROOT, "samples", name))
            state = store.load_state(state_path)
            result, state, _ = app.execute_shift(spec, state)
            store.save_state(state_path, state)
            results[spec["shift_id"]] = result

        s1, s2 = results["S1"], results["S2"]
        sel1 = {s["txn_id"]: s for s in s1["selected"]}
        sel2 = {s["txn_id"]: s for s in s2["selected"]}

        add("S1.selected_ids", "S1 入选名单（保留 T1/T2 + 普通 T4）",
            [s["txn_id"] for s in s1["selected"]], ["T1", "T2", "T4"])
        add("S1.T1.reason", "S1 T1 入选原因=保留名额",
            sel1.get("T1", {}).get("reason"), "reserved_high_risk")
        add("S1.T4.reason", "S1 T4 入选原因=普通名额",
            sel1.get("T4", {}).get("reason"), "general_risk_time")
        add("S1.deferred", "S1 组上限顺延队列（bulkA 上限 2 -> T3 顺延）",
            [r["txn_id"] for r in s1["deferred_cap"]], ["T3"])
        add("S1.deferred.queue", "S1 落选记录保留队列名",
            [r["queue"] for r in s1["deferred_cap"]], ["deferred_cap"])
        add("S1.pending", "S1 名额耗尽落选（低频组 T5 留在 pending）",
            [r["txn_id"] for r in s1["pending"]], ["T5", "T6"])
        add("S2.excluded_claimed", "S2 过滤已被 S1 领取的 T1",
            s2["excluded_claimed"], ["T1"])
        add("S2.T3.reason", "S2 顺延记录 T3 优先补位",
            sel2.get("T3", {}).get("reason"), "carryover_deferred")
        add("S2.T5.group", "S2 低频商户组 rare 的 T5 被补进",
            sel2.get("T5", {}).get("merchant_group"), "rare")
        add("S2.T5.reason", "S2 T5 入选原因=普通名额按风险+入队时间",
            sel2.get("T5", {}).get("reason"), "general_risk_time")
        add("S2.selected_ids", "S2 入选名单（保留 T7 + 顺延 T3 + 普通 T5）",
            [s["txn_id"] for s in s2["selected"]], ["T7", "T3", "T5"])

        # 事务重试：同一 shift_id 复用首次锁定名单
        state = store.load_state(state_path)
        spec2 = app.load_shift_spec(os.path.join(ROOT, "samples", "shift_s2.json"))
        retry, _, reused = app.execute_shift(spec2, state)
        add("S2.retry.reused", "事务重试复用首次锁定名单 (reused_lock)",
            retry["reused_lock"], True)
        add("S2.retry.same_list", "重试名单与首次锁定一致",
            [s["txn_id"] for s in retry["selected"]],
            [s["txn_id"] for s in s2["selected"]])

    all_ok = True
    for c in checks:
        status = "PASS" if c["ok"] else "FAIL"
        if not c["ok"]:
            all_ok = False
        print("[sample] %s %-24s expected=%r actual=%r"
              % (status, c["case_id"], c["expected"], c["actual"]))
    return all_ok


def main():
    ok_tests = run_unit_tests()
    ok_sample = run_sample_assertions()
    if ok_tests and ok_sample:
        print("[verify] OK: 单元测试与样例断言全部通过")
        return 0
    print("[verify] FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
