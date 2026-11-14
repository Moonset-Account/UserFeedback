#!/usr/bin/env python3
"""本地核验入口：运行单元测试套件，并执行已交付样例的逐条断言。

用法：/usr/local/bin/python3 tools/local_verify.py

行为约定：
- 通过 unittest.defaultTestLoader.discover("tests") 获取测试套件；
  发现 0 项、全部跳过、执行异常或存在失败/错误，均以非零退出。
- 随后用 samples/ 下的合成输入实际运行 app.py，逐条比对 expected.json
  中的卷级/设备级期望，打印每条实际结果，并把实际结果写入
  samples/last_verify_results.json 留存。
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


def run_unit_tests():
    print("[verify] 步骤 1/2：发现并运行单元测试 ...")
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    count = suite.countTestCases()
    print(f"[verify] 发现测试用例数: {count}")
    if count < 1:
        print("[verify] 失败: 未发现任何测试用例")
        return False
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    executed = result.testsRun - len(result.skipped)
    print(f"[verify] 实际执行（未跳过）测试数: {executed}")
    if executed < 1:
        print("[verify] 失败: 没有实际执行的测试（全部跳过）")
        return False
    if not result.wasSuccessful():
        print("[verify] 失败: 存在测试失败或错误")
        return False
    return True


def run_sample_assertions():
    print("[verify] 步骤 2/2：运行样例/边界断言 ...")
    expected_doc = json.loads((SAMPLES / "expected.json").read_text(encoding="utf-8"))
    check_time = expected_doc["check_time"]
    cmd = [
        sys.executable,
        str(ROOT / "app.py"),
        "--inventory", str(SAMPLES / "inventory.json"),
        "--reports", str(SAMPLES / "reports.json"),
        "--receipts", str(SAMPLES / "receipts.json"),
        "--check-time", check_time,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        print(f"[verify] 失败: app.py 退出码 {proc.returncode}: {proc.stderr.strip()}")
        return False
    actual = json.loads(proc.stdout)

    vol_index = {}
    dev_index = {}
    for d in actual["devices"]:
        dev_index[d["asset_id"]] = d["status"]
        for v in d["volumes"]:
            vol_index[(d["asset_id"], v["volume"])] = v["status"]

    results = []
    ok = True
    for case in expected_doc.get("volume_cases", []):
        key = (case["asset_id"], case["volume"])
        got = vol_index.get(key)
        want = case["expected_status"]
        passed = got == want
        ok = ok and passed
        results.append({
            "case_id": case["case_id"], "level": "volume",
            "asset_id": case["asset_id"], "volume": case["volume"],
            "expected": want, "actual": got,
            "result": "PASS" if passed else "FAIL",
        })
        print(f"  [{case['case_id']}] {case['asset_id']} {case['volume']}: "
              f"期望 {want} / 实际 {got} -> {'PASS' if passed else 'FAIL'}")
    for case in expected_doc.get("device_cases", []):
        got = dev_index.get(case["asset_id"])
        want = case["expected_status"]
        passed = got == want
        ok = ok and passed
        results.append({
            "case_id": case["case_id"], "level": "device",
            "asset_id": case["asset_id"],
            "expected": want, "actual": got,
            "result": "PASS" if passed else "FAIL",
        })
        print(f"  [{case['case_id']}] 设备 {case['asset_id']}: "
              f"期望 {want} / 实际 {got} -> {'PASS' if passed else 'FAIL'}")

    out_path = SAMPLES / "last_verify_results.json"
    out_path.write_text(
        json.dumps({"check_time": check_time, "cases": results},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[verify] 逐条实际结果已写入 {out_path}")
    if not ok:
        print("[verify] 失败: 存在样例断言不一致")
    return ok


def main():
    try:
        unit_ok = run_unit_tests()
    except Exception as exc:  # noqa: BLE001 - 核验器需如实报告异常
        print(f"[verify] 单元测试阶段异常: {exc}")
        unit_ok = False
    try:
        sample_ok = run_sample_assertions()
    except Exception as exc:  # noqa: BLE001
        print(f"[verify] 样例断言阶段异常: {exc}")
        sample_ok = False

    if unit_ok and sample_ok:
        print("[verify] 总体结果: PASS")
        return 0
    print("[verify] 总体结果: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
