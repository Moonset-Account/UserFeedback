"""SEC-20261114-055 单元测试。

每个用例带稳定 case_id（CASE-055-xx），对应原需求的一条要求：
- CASE-055-01 完整链路：发起 -> 确认 -> 兑换成功
- CASE-055-02 挑战超时（>120s）被拒绝并审计
- CASE-055-03 挑战有效期边界（119s 可确认 / 120s 已过期）
- CASE-055-04 每个挑战最多签发一张许可
- CASE-055-05 MFA 验证失败被拒绝，敏感凭证不进入审计
- CASE-055-06 许可有效期边界（299s 可兑换 / 300s 已过期）
- CASE-055-07 会话切换被拒绝并审计
- CASE-055-08 权限撤回被拒绝并审计
- CASE-055-09 许可已使用（顺序重复兑换）被拒绝并审计
- CASE-055-10 并发兑换同一许可：只有一个请求拿到审计记录
- CASE-055-11 审计记录不含敏感验证凭证（token / proof）
- CASE-055-12 无效 SSO token 在发起与兑换时均被拒绝
"""

import threading
import unittest

from mfa_export import (
    AuditLog,
    ExportLicenseService,
    FakeMFAAdapter,
    FakeSSOAdapter,
    ManualClock,
    Reason,
)

SCOPE = "key-access-audit"
TOKEN_S1 = "token-alice-s1"
TOKEN_S2 = "token-alice-s2"
TOKEN_BOB = "token-bob-s1"
PROOF_OK = "correct-proof"
PROOF_BAD = "wrong-proof"


def make_env(start=1000.0):
    clock = ManualClock(start)
    sso = FakeSSOAdapter()
    sso.register_identity(TOKEN_S1, "tenant-acme", "alice", "sess-1")
    sso.register_identity(TOKEN_S2, "tenant-acme", "alice", "sess-2")
    sso.register_identity(TOKEN_BOB, "tenant-acme", "bob", "sess-9")
    sso.set_export_permission("tenant-acme", "alice", SCOPE, True)
    sso.set_export_permission("tenant-acme", "bob", SCOPE, True)
    mfa = FakeMFAAdapter(default_proof=PROOF_OK)
    audit = AuditLog()
    svc = ExportLicenseService(sso, mfa, clock, audit)
    return svc, clock, sso, mfa, audit


def issue_license(svc):
    init = svc.initiate_mfa(TOKEN_S1, SCOPE)
    assert init["ok"], init
    confirm = svc.confirm_challenge(init["challenge_id"], PROOF_OK)
    assert confirm["ok"], confirm
    return init, confirm


class TestMfaExportLicense(unittest.TestCase):
    def test_case_055_01_happy_path(self):
        """CASE-055-01 完整链路成功，兑换返回租户密钥访问审计记录。"""
        svc, clock, sso, mfa, audit = make_env()
        init, confirm = issue_license(svc)
        self.assertEqual(init["tenant_id"], "tenant-acme")
        self.assertEqual(init["session_id"], "sess-1")
        self.assertEqual(init["scope"], SCOPE)
        redeem = svc.redeem_license(confirm["license_id"], TOKEN_S1)
        self.assertTrue(redeem["ok"], redeem)
        payload = redeem["payload"]
        self.assertEqual(payload["tenant_id"], "tenant-acme")
        self.assertIn("key_access_audit", payload)
        reasons = [r.reason for r in audit.records()]
        self.assertEqual(
            reasons,
            [Reason.OK_CHALLENGE_ISSUED, Reason.OK_LICENSE_ISSUED,
             Reason.OK_LICENSE_REDEEMED],
        )
        for rec in audit.records():
            self.assertEqual(rec.decision, "allow")

    def test_case_055_02_challenge_timeout(self):
        """CASE-055-02 挑战超时：121 秒后确认被拒，审计记录挑战超时。"""
        svc, clock, sso, mfa, audit = make_env()
        init = svc.initiate_mfa(TOKEN_S1, SCOPE)
        clock.advance(121)
        confirm = svc.confirm_challenge(init["challenge_id"], PROOF_OK)
        self.assertFalse(confirm["ok"])
        self.assertEqual(confirm["reason"], Reason.ERR_CHALLENGE_EXPIRED)
        deny = audit.records()[-1]
        self.assertEqual(deny.decision, "deny")
        self.assertEqual(deny.reason, Reason.ERR_CHALLENGE_EXPIRED)
        self.assertEqual(deny.challenge_id, init["challenge_id"])

    def test_case_055_03_challenge_expiry_boundary(self):
        """CASE-055-03 边界：119 秒可确认，恰好 120 秒视为过期。"""
        svc, clock, *_ = make_env()
        init = svc.initiate_mfa(TOKEN_S1, SCOPE)
        clock.advance(119)
        confirm = svc.confirm_challenge(init["challenge_id"], PROOF_OK)
        self.assertTrue(confirm["ok"], confirm)

        svc2, clock2, *_ = make_env()
        init2 = svc2.initiate_mfa(TOKEN_S1, SCOPE)
        clock2.advance(120)
        confirm2 = svc2.confirm_challenge(init2["challenge_id"], PROOF_OK)
        self.assertFalse(confirm2["ok"])
        self.assertEqual(confirm2["reason"], Reason.ERR_CHALLENGE_EXPIRED)

    def test_case_055_04_one_license_per_challenge(self):
        """CASE-055-04 每个挑战最多签发一张许可。"""
        svc, clock, sso, mfa, audit = make_env()
        init = svc.initiate_mfa(TOKEN_S1, SCOPE)
        first = svc.confirm_challenge(init["challenge_id"], PROOF_OK)
        self.assertTrue(first["ok"])
        second = svc.confirm_challenge(init["challenge_id"], PROOF_OK)
        self.assertFalse(second["ok"])
        self.assertEqual(second["reason"], Reason.ERR_CHALLENGE_ALREADY_CONSUMED)
        self.assertEqual(audit.records()[-1].reason,
                         Reason.ERR_CHALLENGE_ALREADY_CONSUMED)

    def test_case_055_05_mfa_verification_failed(self):
        """CASE-055-05 MFA 验证失败被拒；失败后可持正确凭证重试。"""
        svc, clock, sso, mfa, audit = make_env()
        init = svc.initiate_mfa(TOKEN_S1, SCOPE)
        bad = svc.confirm_challenge(init["challenge_id"], PROOF_BAD)
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["reason"], Reason.ERR_MFA_VERIFICATION_FAILED)
        good = svc.confirm_challenge(init["challenge_id"], PROOF_OK)
        self.assertTrue(good["ok"], good)

    def test_case_055_06_license_expiry_boundary(self):
        """CASE-055-06 边界：299 秒可兑换，恰好 300 秒视为过期。"""
        svc, clock, *_ = make_env()
        _, confirm = issue_license(svc)
        clock.advance(299)
        redeem = svc.redeem_license(confirm["license_id"], TOKEN_S1)
        self.assertTrue(redeem["ok"], redeem)

        svc2, clock2, *_ = make_env()
        _, confirm2 = issue_license(svc2)
        clock2.advance(300)
        redeem2 = svc2.redeem_license(confirm2["license_id"], TOKEN_S1)
        self.assertFalse(redeem2["ok"])
        self.assertEqual(redeem2["reason"], Reason.ERR_LICENSE_EXPIRED)

    def test_case_055_07_session_switch(self):
        """CASE-055-07 会话切换：同一用户换会话兑换被拒并审计。"""
        svc, clock, sso, mfa, audit = make_env()
        _, confirm = issue_license(svc)
        redeem = svc.redeem_license(confirm["license_id"], TOKEN_S2)
        self.assertFalse(redeem["ok"])
        self.assertEqual(redeem["reason"], Reason.ERR_SESSION_MISMATCH)
        deny = audit.records()[-1]
        self.assertEqual(deny.decision, "deny")
        self.assertEqual(deny.reason, Reason.ERR_SESSION_MISMATCH)
        self.assertEqual(deny.session_id, "sess-2")

    def test_case_055_08_permission_revoked(self):
        """CASE-055-08 权限撤回：签发后撤回导出权限，兑换被拒并审计。"""
        svc, clock, sso, mfa, audit = make_env()
        _, confirm = issue_license(svc)
        sso.set_export_permission("tenant-acme", "alice", SCOPE, False)
        redeem = svc.redeem_license(confirm["license_id"], TOKEN_S1)
        self.assertFalse(redeem["ok"])
        self.assertEqual(redeem["reason"], Reason.ERR_PERMISSION_REVOKED)
        deny = audit.records()[-1]
        self.assertEqual(deny.decision, "deny")
        self.assertEqual(deny.reason, Reason.ERR_PERMISSION_REVOKED)

    def test_case_055_09_license_already_used(self):
        """CASE-055-09 许可已使用：顺序重复兑换第二次被拒并审计。"""
        svc, clock, sso, mfa, audit = make_env()
        _, confirm = issue_license(svc)
        first = svc.redeem_license(confirm["license_id"], TOKEN_S1)
        self.assertTrue(first["ok"])
        second = svc.redeem_license(confirm["license_id"], TOKEN_S1)
        self.assertFalse(second["ok"])
        self.assertEqual(second["reason"], Reason.ERR_LICENSE_ALREADY_USED)
        self.assertEqual(audit.records()[-1].reason,
                         Reason.ERR_LICENSE_ALREADY_USED)

    def test_case_055_10_concurrent_redeem_single_winner(self):
        """CASE-055-10 并发兑换：16 个请求同时兑换，恰有一个成功。"""
        svc, clock, sso, mfa, audit = make_env()
        _, confirm = issue_license(svc)
        license_id = confirm["license_id"]

        n = 16
        barrier = threading.Barrier(n)
        results = [None] * n

        def worker(i):
            barrier.wait()
            results[i] = svc.redeem_license(license_id, TOKEN_S1)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [r for r in results if r["ok"]]
        losers = [r for r in results if not r["ok"]]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(losers), n - 1)
        for loser in losers:
            self.assertEqual(loser["reason"], Reason.ERR_LICENSE_ALREADY_USED)
        # 只有获胜者拿到载荷
        self.assertEqual(sum(1 for r in results if r.get("payload")), 1)
        redeem_audit = [r for r in audit.records() if r.event == "redeem_license"]
        self.assertEqual(len(redeem_audit), n)
        allowed = [r for r in redeem_audit if r.decision == "allow"]
        denied = [r for r in redeem_audit if r.decision == "deny"]
        self.assertEqual(len(allowed), 1)
        self.assertEqual(allowed[0].reason, Reason.OK_LICENSE_REDEEMED)
        self.assertTrue(all(r.reason == Reason.ERR_LICENSE_ALREADY_USED
                            for r in denied))

    def test_case_055_11_audit_contains_no_secrets(self):
        """CASE-055-11 审计记录不含 SSO token 与 MFA proof。"""
        svc, clock, sso, mfa, audit = make_env()
        init = svc.initiate_mfa(TOKEN_S1, SCOPE)
        svc.confirm_challenge(init["challenge_id"], PROOF_BAD)
        confirm = svc.confirm_challenge(init["challenge_id"], PROOF_OK)
        svc.redeem_license(confirm["license_id"], TOKEN_S1)
        svc.redeem_license(confirm["license_id"], TOKEN_S1)
        blob = repr(audit.to_list())
        for secret in (TOKEN_S1, TOKEN_S2, PROOF_OK, PROOF_BAD):
            self.assertNotIn(secret, blob)

    def test_case_055_12_invalid_sso_token(self):
        """CASE-055-12 无效 SSO token：发起与兑换均被拒并审计。"""
        svc, clock, sso, mfa, audit = make_env()
        init = svc.initiate_mfa("bogus-token", SCOPE)
        self.assertFalse(init["ok"])
        self.assertEqual(init["reason"], Reason.ERR_SSO_INVALID)

        _, confirm = issue_license(svc)
        redeem = svc.redeem_license(confirm["license_id"], "bogus-token")
        self.assertFalse(redeem["ok"])
        self.assertEqual(redeem["reason"], Reason.ERR_SSO_INVALID)
        sso_denies = [r for r in audit.records()
                      if r.reason == Reason.ERR_SSO_INVALID]
        self.assertEqual(len(sso_denies), 2)

    def test_case_055_13_identity_mismatch(self):
        """CASE-055-13 其他用户持有效 token 兑换他人许可被拒。"""
        svc, clock, sso, mfa, audit = make_env()
        _, confirm = issue_license(svc)
        redeem = svc.redeem_license(confirm["license_id"], TOKEN_BOB)
        self.assertFalse(redeem["ok"])
        self.assertEqual(redeem["reason"], Reason.ERR_IDENTITY_MISMATCH)

    def test_case_055_14_initiate_requires_export_permission(self):
        """CASE-055-14 发起时无导出权限被拒并审计。"""
        svc, clock, sso, mfa, audit = make_env()
        init = svc.initiate_mfa(TOKEN_S1, "other-scope")
        self.assertFalse(init["ok"])
        self.assertEqual(init["reason"], Reason.ERR_EXPORT_PERMISSION_DENIED)
        self.assertEqual(audit.records()[-1].decision, "deny")


if __name__ == "__main__":
    unittest.main()
