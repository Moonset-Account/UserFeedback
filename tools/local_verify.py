#!/usr/bin/env python3
"""本地核验入口: 运行 unittest 套件 + 已交付样例/边界断言。

用法: /usr/local/bin/python3 tools/local_verify.py
退出码: 0 全部通过; 非 0 发现 0 项测试、全部跳过、异常、失败或样例断言不通过。
"""
import json
import os
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def run_unit_tests():
    os.chdir(str(ROOT))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover("tests")
    total = suite.countTestCases()
    print("[verify] discovered %d test case(s)" % total)
    if total < 1:
        print("[verify] ERROR: zero tests discovered")
        return False
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    executed = result.testsRun - len(result.skipped)
    print("[verify] executed (not skipped): %d" % executed)
    if executed < 1:
        print("[verify] ERROR: all tests skipped")
        return False
    if not result.wasSuccessful():
        print("[verify] ERROR: unit tests failed")
        return False
    return True


def run_sample_assertions():
    """对已交付的 examples/ 样例逐条断言实际结果。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "app.py"),
         str(ROOT / "examples" / "schema.json"),
         str(ROOT / "examples" / "resolvers.json"),
         str(ROOT / "examples" / "operations.json")],
        capture_output=True, text=True)
    print("[verify] app.py exit code: %d" % proc.returncode)
    if proc.returncode not in (0, 1):
        print("[verify] ERROR: app.py crashed: %s" % proc.stderr)
        return False
    report = json.loads(proc.stdout)

    def paths(group):
        return {f["path"]: f for f in report[group]}

    missing = paths("missing")
    covered = paths("covered")
    manual = paths("manual_review")
    diag = report["diagnostics"]

    checks = [
        ("exit code 1 (missing 存在时 CI 失败)", proc.returncode == 1),
        ("Org 实现遗漏校验 -> GetNode.node.contact missing",
         "GetNode.node.contact" in missing),
        ("missing 结论中 User 仍 covered / Org missing",
         "GetNode.node.contact" in missing
         and {d["type"]: d["status"] for d in missing["GetNode.node.contact"]["applicable"]}
         == {"User": "covered", "Org": "missing"}),
        ("别名 contact 映射回实际字段 ownerEmail",
         "GetNode.node.contact" in missing
         and missing["GetNode.node.contact"]["field"] == "ownerEmail"),
        ("同名别名 contact 在 GetUser 映射到 email 且 covered",
         "GetUser.user.contact" in covered
         and covered["GetUser.user.contact"]["field"] == "email"),
        ("继承证据齐全的 User.internalNote 为 inherited/covered",
         "GetNode.node.internalNote" in manual
         and any(d["type"] == "User" and d["via"] == "inherited"
                 and d["status"] == "covered"
                 for d in manual["GetNode.node.internalNote"]["applicable"])),
        ("继承顺序证据缺失的 Org.internalNote 需人工核对",
         "GetNode.node.internalNote" in manual
         and any(d["type"] == "Org" and d["status"] == "manual_review"
                 and d.get("reason") == "inheritance order evidence missing"
                 for d in manual["GetNode.node.internalNote"]["applicable"])),
        ("缺失解析器元数据 (Profile.ssn) 单独报告",
         any(d["type"] == "Profile" and d["field"] == "ssn"
             for d in diag["missing_resolver_metadata"])),
        ("Profile.ssn 结论为 manual_review",
         "GetUser.user.profile.ssn" in manual),
        ("片段类型条件收窄: GetNode.node.ownerEmail 仅命中 User",
         "GetNode.node.ownerEmail" in covered
         and [d["type"] for d in covered["GetNode.node.ownerEmail"]["applicable"]] == ["User"]),
        ("共享片段在两个根操作独立求值",
         "GetNode.node.ownerEmail" in covered and "GetUser.user.ownerEmail" in covered),
        ("无片段循环误报", diag["fragment_cycles"] == []),
    ]
    ok = True
    for name, passed in checks:
        print("[verify] %s: %s" % ("PASS" if passed else "FAIL", name))
        ok = ok and passed
    return ok


def main():
    ok = run_unit_tests()
    ok = run_sample_assertions() and ok
    print("[verify] overall: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
