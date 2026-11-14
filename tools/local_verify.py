#!/usr/bin/env python3
"""本地核验包装器。

流程：
  1. 用 unittest.defaultTestLoader.discover("tests") 发现测试套件，
     要求 countTestCases() >= 1，随后实际运行；
  2. 要求实际执行且未跳过的测试数 >= 1，存在失败/错误即非零退出；
  3. 运行 app.py 处理 samples/inventory.json，逐条执行样例/边界断言，
     每条打印 case_id、断言内容、期望值与实际值。

任何一步不满足均以非零退出；全部通过才退出 0。
"""
import json
import os
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def run_unit_tests():
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover("tests")
    count = suite.countTestCases()
    print(f"[unit] 发现测试用例数: {count}")
    if count < 1:
        print("[unit] FAIL: 未发现任何测试用例")
        return False
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    executed = result.testsRun - len(result.skipped)
    print(f"[unit] 运行={result.testsRun} 跳过={len(result.skipped)} "
          f"实际执行={executed} 失败={len(result.failures)} 错误={len(result.errors)}")
    if executed < 1:
        print("[unit] FAIL: 没有实际执行且未跳过的测试")
        return False
    if result.failures or result.errors or not result.wasSuccessful():
        print("[unit] FAIL: 存在失败或错误")
        return False
    print("[unit] PASS")
    return True


def _key(report, key_id):
    for entry in report["keys"]:
        if entry["key_id"] == key_id:
            return entry
    raise AssertionError(f"报告缺少密钥 {key_id}")


def run_sample_checks():
    """对已交付样例逐条断言，打印每条的实际结果。"""
    cmd = [sys.executable, str(ROOT / "app.py"), str(ROOT / "samples" / "inventory.json")]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        print(f"[sample] FAIL: app.py 退出码 {proc.returncode}\n{proc.stderr}")
        return False
    report = json.loads(proc.stdout)

    checks = [
        ("SMP-01", "K-RETAIN 停签后仍需保留",
         lambda r: _key(r, "K-RETAIN")["status"], "retain_until"),
        ("SMP-02", "K-RETAIN 最早安全退役时点 = 最晚存量到期(2027-07-04)",
         lambda r: _key(r, "K-RETAIN")["earliest_safe_retirement"],
         "2027-07-04T00:00:00Z"),
        ("SMP-03", "K-RETAIN 存量仅含未重新签发且未到期产物(A-R1, A-R3)",
         lambda r: [i["artifact_id"] for i in _key(r, "K-RETAIN")["retained_stock"]],
         ["A-R1", "A-R3"]),
        ("SMP-04", "K-RETAIN 无开放入口(入口均已停用)",
         lambda r: _key(r, "K-RETAIN")["open_entry_points"], []),
        ("SMP-05", "K-RETAIN 去向已知的引用边不触发人工清点",
         lambda r: _key(r, "K-RETAIN")["manual_review"], []),
        ("SMP-06", "K-RETIRED 完全可退役",
         lambda r: _key(r, "K-RETIRED")["status"], "retirable_now"),
        ("SMP-07", "K-RETIRED 最早退役时点 = 盘点时间",
         lambda r: _key(r, "K-RETIRED")["earliest_safe_retirement"],
         "2026-11-14T00:00:00Z"),
        ("SMP-08", "K-DERIVE 开放入口为 ci-release",
         lambda r: [e["asset_id"] for e in _key(r, "K-DERIVE")["open_entry_points"]],
         ["ci-release"]),
        ("SMP-09", "K-DERIVE 给出通向入口的一条来源链",
         lambda r: _key(r, "K-DERIVE")["open_entry_points"][0]["source_chain"],
         ["app-2.4", "app-2.x-line", "ci-release"]),
        ("SMP-10", "K-DERIVE 入口开放期间不给退役时点",
         lambda r: (_key(r, "K-DERIVE")["earliest_safe_retirement"],
                    _key(r, "K-DERIVE")["blocking_reasons"]),
         (None, ["open_entry_points"])),
        ("SMP-11", "K-MANUAL 缺失完成时间+去向不明 -> 人工清点",
         lambda r: _key(r, "K-MANUAL")["status"], "manual_review"),
        ("SMP-12", "K-MANUAL 人工清点原因齐全",
         lambda r: [m["reason"] for m in _key(r, "K-MANUAL")["manual_review"]],
         ["reference_destination_unknown", "reissue_completion_missing"]),
        ("SMP-13", "K-MANUAL 未完成重新签发的产物仍在存量中",
         lambda r: [i["artifact_id"] for i in _key(r, "K-MANUAL")["retained_stock"]],
         ["A-M1"]),
        ("SMP-14", "报告按输入顺序覆盖全部 4 把密钥",
         lambda r: [k["key_id"] for k in r["keys"]],
         ["K-RETAIN", "K-RETIRED", "K-DERIVE", "K-MANUAL"]),
    ]

    ok = True
    for case_id, desc, fn, expected in checks:
        try:
            actual = fn(report)
        except Exception as exc:  # noqa: BLE001 - 逐条记录异常结果
            actual = f"<exception: {exc}>"
        passed = actual == expected
        ok = ok and passed
        verdict = "PASS" if passed else "FAIL"
        print(f"[sample] {case_id} {verdict} | {desc} | expected={expected!r} actual={actual!r}")
    return ok


def main():
    unit_ok = run_unit_tests()
    sample_ok = run_sample_checks()
    if unit_ok and sample_ok:
        print("VERIFY RESULT: PASS")
        return 0
    print("VERIFY RESULT: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
