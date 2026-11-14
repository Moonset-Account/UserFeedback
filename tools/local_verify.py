#!/usr/bin/env python3
"""本地核验入口（SEC-20261114-055）。

流程：
1. 用 unittest.defaultTestLoader.discover("tests") 发现测试套件，
   要求 countTestCases() >= 1，随后实际运行；
   要求实际执行且未跳过的测试数 >= 1；
   发现 0 项、全部跳过、异常或任何失败 -> 非零退出。
2. 运行本题交付的样例/边界断言（通过 app.py 的场景入口），
   逐条打印 case_id、断言内容、期望值与实际结果；任一失败 -> 非零退出。

用法：/usr/local/bin/python3 tools/local_verify.py
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def run_unit_tests() -> bool:
    print("== 单元测试 ==")
    suite = unittest.defaultTestLoader.discover("tests")
    count = suite.countTestCases()
    print("discovered test cases: %d" % count)
    if count < 1:
        print("FAIL: 未发现任何测试用例")
        return False
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    executed = result.testsRun - len(result.skipped)
    print("testsRun=%d skipped=%d executed=%d failures=%d errors=%d"
          % (result.testsRun, len(result.skipped), executed,
             len(result.failures), len(result.errors)))
    if executed < 1:
        print("FAIL: 实际执行且未跳过的测试数为 0")
        return False
    if not result.wasSuccessful():
        print("FAIL: 存在失败或错误的测试")
        return False
    print("单元测试: PASS")
    return True


def run_sample_assertions() -> bool:
    print("== 样例/边界断言（经由 app.py 场景入口） ==")
    import app
    from mfa_export import Reason

    checks = []

    def check(case_id, description, actual, expected):
        ok = actual == expected
        checks.append(ok)
        status = "PASS" if ok else "FAIL"
        print("[%s] %s | %s | expected=%r actual=%r"
              % (case_id, status, description, expected, actual))

    # 完整链路：兑换成功且只有一条允许记录
    r = app.scenario_happy_path()
    check("SEC055-S01", "完整链路兑换成功", r["steps"]["redeem"]["ok"], True)
    check("SEC055-S02", "载荷为租户密钥访问审计记录",
          r["steps"]["redeem"]["payload"]["tenant_id"], "tenant-acme")

    # 挑战超时
    r = app.scenario_challenge_timeout()
    check("SEC055-S03", "挑战超时原因码", r["steps"]["confirm"]["reason"],
          Reason.ERR_CHALLENGE_EXPIRED)

    # MFA 验证失败
    r = app.scenario_mfa_failure()
    check("SEC055-S04", "MFA 验证失败原因码", r["steps"]["confirm"]["reason"],
          Reason.ERR_MFA_VERIFICATION_FAILED)

    # 一挑战一许可
    r = app.scenario_one_license_per_challenge()
    check("SEC055-S05", "第二次确认被拒原因码", r["steps"]["second"]["reason"],
          Reason.ERR_CHALLENGE_ALREADY_CONSUMED)

    # 许可过期
    r = app.scenario_license_expired()
    check("SEC055-S06", "许可过期原因码", r["steps"]["redeem"]["reason"],
          Reason.ERR_LICENSE_EXPIRED)

    # 会话切换
    r = app.scenario_session_switch()
    check("SEC055-S07", "会话切换原因码", r["steps"]["redeem"]["reason"],
          Reason.ERR_SESSION_MISMATCH)

    # 权限撤回
    r = app.scenario_permission_revoked()
    check("SEC055-S08", "权限撤回原因码", r["steps"]["redeem"]["reason"],
          Reason.ERR_PERMISSION_REVOKED)

    # 许可已使用（顺序）
    r = app.scenario_license_reused()
    check("SEC055-S09", "重复兑换原因码", r["steps"]["second"]["reason"],
          Reason.ERR_LICENSE_ALREADY_USED)

    # 并发兑换：恰有一个获胜者
    r = app.scenario_concurrent_redeem()
    check("SEC055-S10", "并发兑换获胜者数量", r["steps"]["winners"], 1)
    check("SEC055-S11", "并发兑换载荷交付数",
          r["steps"]["payloads_delivered"], 1)
    check("SEC055-S12", "并发失败方原因码集合",
          sorted(set(r["steps"]["deny_reasons"])),
          [Reason.ERR_LICENSE_ALREADY_USED])

    # 审计不含敏感凭证
    r = app.scenario_happy_path()
    blob = repr(r["audit"])
    leaked = [s for s in (app.TOKEN_S1, app.TOKEN_S2, app.PROOF_OK,
                          app.PROOF_BAD) if s in blob]
    check("SEC055-S13", "审计记录不含敏感凭证", leaked, [])

    # 审计记录包含判定结果与原因
    r = app.scenario_permission_revoked()
    fields_ok = all(
        rec.get("decision") in ("allow", "deny") and bool(rec.get("reason"))
        for rec in r["audit"]
    )
    check("SEC055-S14", "每条审计含判定结果与原因", fields_ok, True)

    passed = sum(1 for c in checks if c)
    print("样例/边界断言: %d/%d 通过" % (passed, len(checks)))
    return all(checks)


def main() -> int:
    ok_tests = run_unit_tests()
    ok_samples = run_sample_assertions()
    if ok_tests and ok_samples:
        print("LOCAL VERIFY: PASS")
        return 0
    print("LOCAL VERIFY: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
