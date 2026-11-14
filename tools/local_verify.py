#!/usr/bin/env python3
"""本地核验入口（SEC-20261114-024）。

两步核验，任一步失败即以非零退出：
1) 用 unittest.defaultTestLoader.discover("tests") 发现测试套件并实际运行；
   发现 0 项、实际执行且未跳过 0 项、异常或任何失败均判失败，
   不把 unittest 的零测试退出 0 当作通过。
2) 对已交付样例 samples/manifest.json 逐条执行边界断言，
   每条保留 case_id、断言内容、实际结果与期望结果。

用法：/usr/local/bin/python3 tools/local_verify.py
"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def run_unit_suite():
    print("== 第一步：单元测试套件 ==")
    suite = unittest.defaultTestLoader.discover(
        os.path.join(ROOT, "tests"), top_level_dir=ROOT
    )
    total = suite.countTestCases()
    print("发现测试用例数: %d" % total)
    if total < 1:
        print("FAIL: 未发现任何测试用例")
        return False
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    executed = result.testsRun - len(result.skipped)
    print("实际执行且未跳过的测试数: %d" % executed)
    if executed < 1:
        print("FAIL: 没有实际执行且未跳过的测试")
        return False
    if result.failures or result.errors or not result.wasSuccessful():
        print("FAIL: 存在失败或错误的测试")
        return False
    print("单元测试全部通过")
    return True


def _run_cli(*argv):
    return subprocess.run(
        [PY, os.path.join(ROOT, "app.py")] + list(argv),
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
    )


def run_sample_checks():
    print("== 第二步：样例/边界断言（samples/manifest.json） ==")
    failures = []

    def record(case_id, desc, actual, expected):
        ok = actual == expected
        print(
            "%s | %s | 实际=%r | 期望=%r | %s"
            % (case_id, desc, actual, expected, "PASS" if ok else "FAIL")
        )
        if not ok:
            failures.append(case_id)
        return ok

    proc = _run_cli("classify", "samples/manifest.json")
    if not record("SC-00", "classify 子命令退出码", proc.returncode, 0):
        print(proc.stderr)
        return False
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        print("FAIL: classify 输出不是合法 JSON")
        return False

    fields = report.get("fields", {})
    dg = report.get("downgrades", {})

    record(
        "SC-01",
        "降级规则命中：mart.customer_profile 按 DG-1 降至 S2",
        fields.get("mart.customer_profile", {}).get("level"),
        "S2",
    )
    record(
        "SC-02",
        "DG-1 出现在已生效规则列表",
        dg.get("applied"),
        ["DG-1"],
    )
    record(
        "SC-03",
        "DG-2 批准范围（输入等级 S3）与实际 S2 不符，判为未命中",
        [u["id"] for u in dg.get("unmatched", [])],
        ["DG-2"],
    )
    record(
        "SC-04",
        "未命中规则给出原因（含期望/实际等级）",
        "期望等级 S3，实际 S2" in (dg.get("unmatched", [{}])[0].get("reason", "")),
        True,
    )
    record(
        "SC-05",
        "一般派生取输入最高等级：mart.region_revenue = S2",
        fields.get("mart.region_revenue", {}).get("level"),
        "S2",
    )
    record(
        "SC-06",
        "血缘缺边字段 ext.unknown 标记待核对",
        fields.get("ext.unknown", {}).get("status"),
        "pending_review",
    )
    record(
        "SC-07",
        "待核对沿血缘传播：mart.mystery 同样待核对",
        fields.get("mart.mystery", {}).get("status"),
        "pending_review",
    )
    record(
        "SC-08",
        "环中字段迭代到稳定等级 S2",
        [
            fields.get("feed.p", {}).get("level"),
            fields.get("feed.q", {}).get("level"),
        ],
        ["S2", "S2"],
    )
    record(
        "SC-09",
        "报告列出环涉及的字段",
        report.get("cycles"),
        [["feed.p", "feed.q"]],
    )
    record(
        "SC-10",
        "每个字段都带一条非空解释来源",
        all(
            isinstance(v.get("explanation"), str) and v["explanation"].strip()
            for v in fields.values()
        ) and len(fields) > 0,
        True,
    )

    proc2 = _run_cli("revoke", "samples/manifest.json", "DG-1")
    if not record("SC-11", "revoke 子命令退出码", proc2.returncode, 0):
        print(proc2.stderr)
        return False
    result = json.loads(proc2.stdout)
    record(
        "SC-12",
        "撤销 DG-1 后受影响下游字段列表",
        [a["field"] for a in result.get("affected_fields", [])],
        ["mart.customer_profile", "mart.vip_list"],
    )
    record(
        "SC-13",
        "受影响字段等级变化 S2 -> S4",
        [
            (a["before"]["level"], a["after"]["level"])
            for a in result.get("affected_fields", [])
        ],
        [("S2", "S4"), ("S2", "S4")],
    )
    record(
        "SC-14",
        "撤销不存在的规则返回非零退出码",
        _run_cli("revoke", "samples/manifest.json", "DG-404").returncode,
        2,
    )

    if failures:
        print("样例断言失败: %s" % ", ".join(failures))
        return False
    print("样例断言全部通过")
    return True


def main():
    ok_unit = run_unit_suite()
    ok_sample = run_sample_checks()
    if ok_unit and ok_sample:
        print("LOCAL VERIFY: PASS")
        return 0
    print("LOCAL VERIFY: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
