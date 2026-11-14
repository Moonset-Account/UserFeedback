#!/usr/bin/env python3
"""本地核验入口。

流程:
1. 用 unittest.defaultTestLoader.discover("tests") 获取测试套件,
   要求 countTestCases() >= 1, 然后实际运行;
2. 要求实际执行且未跳过的测试数 >= 1, 且没有失败/错误;
3. 运行 app.py 对随附样例做逐条边界断言, 保留每条实际结果;
4. 以上全部通过才以 0 退出, 否则非零退出。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def run_unit_tests():
    os.chdir(ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    total = suite.countTestCases()
    print("[verify] discover('tests') 发现测试用例 {0} 个".format(total))
    if total < 1:
        print("[verify] FAIL: 未发现任何测试用例")
        return False
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    executed = result.testsRun - len(result.skipped)
    print("[verify] 执行 {0}, 跳过 {1}, 失败 {2}, 错误 {3}".format(
        result.testsRun, len(result.skipped),
        len(result.failures), len(result.errors)))
    if executed < 1:
        print("[verify] FAIL: 实际执行且未跳过的测试数为 0")
        return False
    if not result.wasSuccessful():
        print("[verify] FAIL: 存在失败或错误的测试")
        return False
    return True


def run_sample_assertions():
    """对随附 data/ 样例逐条断言, 每条打印实际值与期望值。"""
    checks = []
    with tempfile.TemporaryDirectory() as tmp:
        reports = []
        for name in ("report1.json", "report2.json"):
            out = os.path.join(tmp, name)
            proc = subprocess.run(
                [sys.executable, os.path.join(ROOT, "app.py"),
                 "--data", os.path.join(ROOT, "data"), "--out", out],
                capture_output=True, text=True, cwd=ROOT)
            if proc.returncode != 0:
                print("[verify] FAIL: app.py 退出码 {0}: {1}".format(
                    proc.returncode, proc.stderr.strip()))
                return False
            with open(out, encoding="utf-8") as fh:
                reports.append(json.load(fh))
    report = reports[0]

    def check(case_id, actual, expected):
        ok = actual == expected
        checks.append(ok)
        print("[sample {0}] {1} actual={2!r} expected={3!r}".format(
            case_id, "PASS" if ok else "FAIL", actual, expected))

    check("determinism.repeat-run", reports[0] == reports[1], True)

    modes = report["modes"]
    rel = modes["release"]["symbols"]
    check("release.parse_token.status",
          rel["acme@1.4.0:parse_token"]["status"], "reachable")
    check("release.parse_token.path",
          rel["acme@1.4.0:parse_token"]["path"],
          ["plugin.loader:load", "plugkit@0.9.2:handle",
           "plugkit@0.9.2:emit", "acme@1.4.0:parse_token"])
    check("release.deserialize.status",
          rel["acme@1.4.0:deserialize"]["status"], "unreachable")
    check("release.evidence_complete",
          modes["release"]["evidence_complete"], True)

    tst = modes["test"]["symbols"]
    check("test.deserialize.status",
          tst["acme@1.4.0:deserialize"]["status"], "reachable")
    check("test.parse_token.status",
          tst["acme@1.4.0:parse_token"]["status"], "reachable")

    dev = modes["dev"]["symbols"]
    check("dev.parse_token.status",
          dev["acme@1.4.0:parse_token"]["status"], "insufficient_evidence")
    check("dev.deserialize.status",
          dev["acme@1.4.0:deserialize"]["status"], "insufficient_evidence")
    check("dev.missing_evidence.mentions_dynamic",
          any("tools.repl:main" in m
              for m in dev["acme@1.4.0:parse_token"]["missing_evidence"]),
          True)

    check("priority_fixes",
          [(f["entry"], f["symbol"]) for f in report["priority_fixes"]],
          [("plugin.loader:load", "acme@1.4.0:parse_token")])

    if not checks:
        print("[verify] FAIL: 样例断言数量为 0")
        return False
    return all(checks)


def main():
    ok_tests = run_unit_tests()
    ok_samples = run_sample_assertions()
    if ok_tests and ok_samples:
        print("[verify] 全部核验通过")
        return 0
    print("[verify] 核验未通过")
    return 1


if __name__ == "__main__":
    sys.exit(main())
