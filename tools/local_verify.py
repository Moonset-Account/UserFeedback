#!/usr/bin/env python3
"""本地核验入口（有限时间退出）。

步骤：
1) 用 unittest.defaultTestLoader.discover("tests") 发现测试套件，要求
   countTestCases() >= 1，随后实际运行；实际执行且未跳过的测试数必须 >= 1；
   发现 0 项、全部跳过、异常或任何失败都以非零退出。
2) 逐条执行本题样例断言（samples/）与边界断言，打印每条的实际结果与期望结果。

运行：/usr/local/bin/python3 tools/local_verify.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

failures = []


def check(case_id, actual, expected):
    ok = actual == expected
    print(f"[{case_id}] expected={expected!r} actual={actual!r} -> {'PASS' if ok else 'FAIL'}")
    if not ok:
        failures.append(case_id)
    return ok


def run_unit_tests():
    os.chdir(ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    total = suite.countTestCases()
    print(f"[verify] discover('tests') 发现测试数: {total}")
    if total < 1:
        print("[verify] 失败: 未发现任何测试用例")
        return False
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    executed = result.testsRun - len(result.skipped)
    print(f"[verify] 实际执行且未跳过的测试数: {executed}")
    if executed < 1:
        print("[verify] 失败: 没有实际执行（未跳过）的测试")
        return False
    if not result.wasSuccessful():
        print("[verify] 失败: 存在失败或错误的测试")
        return False
    return True


def run_cli(collection_obj, archive_obj):
    with tempfile.TemporaryDirectory() as tmp:
        cpath = Path(tmp) / "collection.json"
        apath = Path(tmp) / "archive.json"
        cpath.write_text(json.dumps(collection_obj), encoding="utf-8")
        apath.write_text(json.dumps(archive_obj), encoding="utf-8")
        proc = subprocess.run(
            [PYTHON, str(ROOT / "app.py"), "--collection", str(cpath), "--archive", str(apath)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    if proc.returncode != 0:
        raise RuntimeError(f"CLI 退出码 {proc.returncode}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def run_sample_checks():
    print("[verify] --- 样例断言（两段缺口 + 一处摘要不一致） ---")
    proc = subprocess.run(
        [
            PYTHON,
            str(ROOT / "app.py"),
            "--collection",
            str(ROOT / "samples" / "collection.json"),
            "--archive",
            str(ROOT / "samples" / "archive.json"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    check("SAMPLE-EXIT-CODE", proc.returncode, 0)
    if proc.returncode != 0:
        print(proc.stderr)
        return
    report = json.loads(proc.stdout)
    expected = json.loads((ROOT / "samples" / "expected_report.json").read_text(encoding="utf-8"))
    check("SAMPLE-REPORT-EQUAL", report, expected)
    group = report["groups"][0]
    check("SAMPLE-WATERMARK", group["watermark"], 2)
    check("SAMPLE-GAPS", group["gaps"], [{"start": 4, "end": 5}, {"start": 7, "end": 7}])
    check("SAMPLE-MISMATCH-SEQS", [m["seq"] for m in group["digest_mismatches"]], [3])
    check("SAMPLE-EXTRA-SEQS", [e["seq"] for e in group["archive_extras"]], [9])
    check(
        "SAMPLE-PLAN",
        [(p["seq"], p["reason"]) for p in group["retransmit_plan"]],
        [(3, "digest_mismatch"), (4, "missing"), (5, "missing"), (7, "missing")],
    )


def run_boundary_checks():
    print("[verify] --- 边界断言 ---")
    # 边界 1：归档端为空 -> 水位停在 start_seq - 1，全部期望序号进入缺口与补传计划
    coll = {
        "records": [
            {"tenant": "t", "shard": "s", "seq": i, "digest": f"d{i}", "status": "received"}
            for i in (1, 2, 3)
        ]
    }
    group = run_cli(coll, {"records": []})["groups"][0]
    check("BOUND-EMPTY-ARCHIVE-WATERMARK", group["watermark"], 0)
    check("BOUND-EMPTY-ARCHIVE-GAPS", group["gaps"], [{"start": 1, "end": 3}])
    check("BOUND-EMPTY-ARCHIVE-PLAN", [p["seq"] for p in group["retransmit_plan"]], [1, 2, 3])

    # 边界 2：归档端存在采集端没有的组 -> 全部记为多出，水位为 None，计划为空
    group = run_cli(
        {"records": []},
        {"records": [{"tenant": "t2", "shard": "s9", "seq": 5, "digest": "x", "status": "persisted"}]},
    )["groups"][0]
    check("BOUND-EXTRA-GROUP-WATERMARK", group["watermark"], None)
    check("BOUND-EXTRA-GROUP-EXTRAS", [e["seq"] for e in group["archive_extras"]], [5])
    check("BOUND-EXTRA-GROUP-PLAN", group["retransmit_plan"], [])

    # 边界 3：起始序号未确认 -> 水位为 start_seq - 1，不越过缺口
    group = run_cli(
        {"records": [{"tenant": "t", "shard": "s", "seq": i, "digest": f"d{i}", "status": "received"} for i in (5, 6)]},
        {"records": [{"tenant": "t", "shard": "s", "seq": 6, "digest": "d6", "status": "persisted"}]},
    )["groups"][0]
    check("BOUND-START-UNCONFIRMED-WATERMARK", group["watermark"], 4)
    check("BOUND-START-UNCONFIRMED-GAPS", group["gaps"], [{"start": 5, "end": 5}])


def main():
    ok = run_unit_tests()
    try:
        run_sample_checks()
        run_boundary_checks()
    except Exception as exc:  # 样例/边界断言执行异常同样判失败
        print(f"[verify] 样例/边界断言执行异常: {exc}")
        failures.append("SAMPLE/BOUNDARY-EXCEPTION")
    if failures:
        ok = False
    print(f"[verify] 总体结果: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
