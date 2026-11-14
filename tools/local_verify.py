#!/usr/bin/env python3
"""本地核验入口（仅标准库）。

用法：/usr/local/bin/python3 tools/local_verify.py

流程：
1. 用 unittest.defaultTestLoader.discover("tests") 发现测试套件，
   要求 countTestCases() >= 1，随后实际运行；
   实际执行且未跳过的测试数必须 >= 1；
   发现 0 项、全部跳过、异常或任何失败均以非零退出。
2. 逐条执行本题样例/边界断言（SAMPLE-xx），保留每条实际结果，
   任何一条失败均以非零退出。
"""

import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_unit_tests():
    os.chdir(ROOT)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    total = suite.countTestCases()
    print(f"[verify] 发现测试用例数: {total}")
    if total < 1:
        print("[verify] 失败: 未发现任何测试用例")
        return False
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    executed = result.testsRun - len(result.skipped)
    print(f"[verify] 实际执行(未跳过)测试数: {executed}")
    if executed < 1:
        print("[verify] 失败: 没有实际执行的测试（全部跳过或未运行）")
        return False
    if result.failures or result.errors:
        print(f"[verify] 失败: failures={len(result.failures)} errors={len(result.errors)}")
        return False
    print("[verify] 单元测试全部通过")
    return True


def check(results, case_id, desc, expected, actual):
    ok = expected == actual
    results.append(ok)
    print(f"[{case_id}] {desc}")
    print(f"  期望: {expected}")
    print(f"  实际: {actual}")
    print(f"  结果: {'PASS' if ok else 'FAIL'}")


def run_cli(*args):
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "app.py"), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )
    return proc


def final_line(stdout, device):
    prefix = f"FINAL device={device} "
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line.strip()
    return None


def run_sample_assertions():
    results = []

    # SAMPLE-01: 内置 demo —— 先隔离再解除、解除回执先到，最终已解除
    proc = run_cli("demo")
    check(
        results,
        "SAMPLE-01",
        "demo 退出码为 0",
        0,
        proc.returncode,
    )
    check(
        results,
        "SAMPLE-02",
        "demo 中 dev-01 最终状态（解除回执先到，隔离回执不覆盖）",
        "FINAL device=dev-01 expected=released confirmed=released pending=0",
        final_line(proc.stdout, "dev-01"),
    )
    check(
        results,
        "SAMPLE-03",
        "demo 中 dev-02 最终状态（失败重发后隔离生效）",
        "FINAL device=dev-02 expected=isolated confirmed=isolated pending=0",
        final_line(proc.stdout, "dev-02"),
    )

    # SAMPLE-04: 样例事件流文件可独立重跑出同样结论
    proc2 = run_cli("run", os.path.join("samples", "out_of_order_events.jsonl"))
    check(
        results,
        "SAMPLE-04",
        "samples/out_of_order_events.jsonl 重跑最终状态一致",
        "FINAL device=dev-01 expected=released confirmed=released pending=0",
        final_line(proc2.stdout, "dev-01"),
    )

    # SAMPLE-05: 模块级断言 —— 重发沿用原操作序号并保留发送历史
    from isolation_module import IsolationConsole

    c = IsolationConsole()
    c.request("dev-x", "isolate")
    c.handle_receipt("dev-x", 1, "failed")
    rec = c.resend("dev-x")
    history = c.send_history("dev-x")
    check(
        results,
        "SAMPLE-05",
        "重发沿用原 op_seq=1、新 cmd_id=2、历史保留 2 条",
        (1, 2, 2, ["ack_failed", "pending"]),
        (rec.op_seq, rec.cmd_id, len(history), [h["status"] for h in history]),
    )

    # SAMPLE-06: 模块级断言 —— 旧操作成功回执不覆盖已确认的新状态
    c2 = IsolationConsole()
    c2.request("dev-y", "isolate")   # cmd 1, op 1
    c2.request("dev-y", "release")   # cmd 2, op 2
    c2.handle_receipt("dev-y", 2, "success")
    out = c2.handle_receipt("dev-y", 1, "success")
    view = c2.console_view("dev-y")
    check(
        results,
        "SAMPLE-06",
        "解除回执先确认后，隔离成功回执仅补历史（applied=False，确认状态保持 released）",
        (False, "released", 2),
        (out["applied"], view["last_confirmed_state"], view["confirmed_op_seq"]),
    )

    return all(results)


def main():
    ok_tests = run_unit_tests()
    print("-" * 60)
    ok_samples = run_sample_assertions()
    print("-" * 60)
    if ok_tests and ok_samples:
        print("[verify] 总体结论: PASS")
        return 0
    print(f"[verify] 总体结论: FAIL (unit_tests={ok_tests}, samples={ok_samples})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
