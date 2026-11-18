"""账户关联模块验收测试（unittest）。

覆盖题面交叉约束：
- 可接受结果：双方分别完成有效证明、用户指定保留账户、确认合并后
  两个原身份登录解析到同一保留账户且保有来源记录。
- 应被阻止的结果：外部身份已关联到其他保留账户；证明过期/未验证；
  保留账户指定非法；证明未齐备即确认。
- 边界/交错：确认与任一方撤回并发时只产生一种最终结果；
  终态之后的任何操作被拒绝；邮箱相同仅提示候选而不自动关联。
"""

import threading
import unittest
from datetime import datetime, timedelta

from account_linking import (
    AccountLinkingService,
    IdentityAlreadyLinkedError,
    InvalidKeepAccountError,
    OwnershipProof,
    ProofExpiredError,
    ProofInvalidError,
    SessionFinalizedError,
    SessionNotReadyError,
    SessionStatus,
)

NOW = datetime(2026, 11, 19, 12, 0, 0)
FUTURE = NOW + timedelta(hours=1)
PAST = NOW - timedelta(hours=1)


def make_service_with_two_accounts(email_shared=True):
    """构造：机构甲账户 A(identity id_a)、机构乙账户 B(identity id_b)。"""
    svc = AccountLinkingService()
    email = "employee@example.com" if email_shared else "a@example.com"
    svc.register_account("acc_a", email)
    svc.register_account("acc_b", "employee@example.com")
    svc.register_identity("id_a", "idp-alpha", "sub-a", "acc_a")
    svc.register_identity("id_b", "idp-beta", "sub-b", "acc_b")
    return svc


def valid_proof(identity_id):
    return OwnershipProof(identity_id=identity_id, verified=True, expires_at=FUTURE)


class AcceptableFlowTest(unittest.TestCase):
    """可接受结果：完整合并流程。"""

    def test_full_merge_flow_resolves_to_same_retained_account(self):
        svc = make_service_with_two_accounts()
        session = svc.start_link_session("acc_a", "acc_b", keep_account_id="acc_a")
        svc.submit_proof(session.session_id, valid_proof("id_a"), now=NOW)
        svc.submit_proof(session.session_id, valid_proof("id_b"), now=NOW)
        result = svc.confirm(session.session_id, now=NOW)

        self.assertEqual(result.status, SessionStatus.CONFIRMED)

        # 两个原身份登录都解析到同一保留账户
        retained_a, prov_a = svc.resolve_login("id_a")
        retained_b, prov_b = svc.resolve_login("id_b")
        self.assertEqual(retained_a, "acc_a")
        self.assertEqual(retained_b, "acc_a")

        # 来源记录：id_b 从 acc_b 并入 acc_a
        sources = {(r.identity_id, r.source_account_id, r.retained_account_id)
                   for r in prov_b}
        self.assertIn(("id_b", "acc_b", "acc_a"), sources)
        self.assertEqual(prov_a, prov_b)

    def test_keep_account_can_be_either_side(self):
        svc = make_service_with_two_accounts()
        session = svc.start_link_session("acc_a", "acc_b", keep_account_id="acc_b")
        svc.submit_proof(session.session_id, valid_proof("id_a"), now=NOW)
        svc.submit_proof(session.session_id, valid_proof("id_b"), now=NOW)
        svc.confirm(session.session_id, now=NOW)
        retained, _ = svc.resolve_login("id_a")
        self.assertEqual(retained, "acc_b")


class CandidateHintTest(unittest.TestCase):
    """邮箱相同只能提示候选，不得自动关联。"""

    def test_same_email_only_suggests_candidates(self):
        svc = make_service_with_two_accounts()
        candidates = svc.suggest_candidates("employee@example.com")
        self.assertEqual({a.account_id for a in candidates}, {"acc_a", "acc_b"})
        # 提示之后双方仍是独立账户，没有任何关联发生
        self.assertEqual(svc.resolve_login("id_a")[0], "acc_a")
        self.assertEqual(svc.resolve_login("id_b")[0], "acc_b")
        self.assertEqual(svc.resolve_login("id_a")[1], [])


class BlockedFlowTest(unittest.TestCase):
    """应被阻止或拒绝的结果。"""

    def test_identity_already_linked_to_other_retained_account_blocks_session(self):
        svc = make_service_with_two_accounts()
        svc.register_account("acc_c", "c@example.com")
        svc.register_identity("id_c", "idp-gamma", "sub-c", "acc_c")
        # 第一次合并：B 并入 A
        s1 = svc.start_link_session("acc_a", "acc_b", keep_account_id="acc_a")
        svc.submit_proof(s1.session_id, valid_proof("id_a"), now=NOW)
        svc.submit_proof(s1.session_id, valid_proof("id_b"), now=NOW)
        svc.confirm(s1.session_id, now=NOW)
        # 已并入 A 的 B 再参与新关联 -> 阻止
        with self.assertRaises(IdentityAlreadyLinkedError):
            svc.start_link_session("acc_b", "acc_c", keep_account_id="acc_c")

    def test_confirm_time_guard_blocks_identity_bound_elsewhere(self):
        """确认时刻的防御性检查：身份在会话期间被其他保留账户占用。"""
        svc = make_service_with_two_accounts()
        session = svc.start_link_session("acc_a", "acc_b", keep_account_id="acc_a")
        svc.submit_proof(session.session_id, valid_proof("id_a"), now=NOW)
        svc.submit_proof(session.session_id, valid_proof("id_b"), now=NOW)
        # 模拟并发窗口内 id_b 被另一保留账户占用
        svc._identity_account["id_b"] = "acc_other"
        with self.assertRaises(IdentityAlreadyLinkedError):
            svc.confirm(session.session_id, now=NOW)
        # 阻止后会话不得落成已确认
        self.assertEqual(svc._sessions[session.session_id].status,
                         SessionStatus.PENDING)

    def test_expired_proof_rejected(self):
        svc = make_service_with_two_accounts()
        session = svc.start_link_session("acc_a", "acc_b", keep_account_id="acc_a")
        expired = OwnershipProof("id_a", verified=True, expires_at=PAST)
        with self.assertRaises(ProofExpiredError):
            svc.submit_proof(session.session_id, expired, now=NOW)

    def test_unverified_proof_rejected(self):
        svc = make_service_with_two_accounts()
        session = svc.start_link_session("acc_a", "acc_b", keep_account_id="acc_a")
        forged = OwnershipProof("id_a", verified=False, expires_at=FUTURE)
        with self.assertRaises(ProofInvalidError):
            svc.submit_proof(session.session_id, forged, now=NOW)

    def test_proof_expiring_before_confirm_rejected(self):
        """提交时有效、确认时已过期：边界场景，确认必须再次校验。"""
        svc = make_service_with_two_accounts()
        session = svc.start_link_session("acc_a", "acc_b", keep_account_id="acc_a")
        soon = NOW + timedelta(minutes=5)
        svc.submit_proof(session.session_id,
                         OwnershipProof("id_a", True, soon), now=NOW)
        svc.submit_proof(session.session_id, valid_proof("id_b"), now=NOW)
        later = NOW + timedelta(minutes=10)
        with self.assertRaises(ProofExpiredError):
            svc.confirm(session.session_id, now=later)

    def test_confirm_requires_both_proofs(self):
        svc = make_service_with_two_accounts()
        session = svc.start_link_session("acc_a", "acc_b", keep_account_id="acc_a")
        svc.submit_proof(session.session_id, valid_proof("id_a"), now=NOW)
        with self.assertRaises(SessionNotReadyError):
            svc.confirm(session.session_id, now=NOW)

    def test_proof_from_outsider_identity_rejected(self):
        svc = make_service_with_two_accounts()
        svc.register_account("acc_x", "x@example.com")
        svc.register_identity("id_x", "idp-x", "sub-x", "acc_x")
        session = svc.start_link_session("acc_a", "acc_b", keep_account_id="acc_a")
        with self.assertRaises(ProofInvalidError):
            svc.submit_proof(session.session_id, valid_proof("id_x"), now=NOW)

    def test_keep_account_must_be_one_of_the_two(self):
        svc = make_service_with_two_accounts()
        svc.register_account("acc_c", "c@example.com")
        with self.assertRaises(InvalidKeepAccountError):
            svc.start_link_session("acc_a", "acc_b", keep_account_id="acc_c")
        with self.assertRaises(InvalidKeepAccountError):
            svc.start_link_session("acc_a", "acc_a", keep_account_id="acc_a")


class ConcurrentFinalizationTest(unittest.TestCase):
    """边界/交错：确认与撤回同时发生，只产生一种最终结果。"""

    def _prepared_session(self, svc):
        session = svc.start_link_session("acc_a", "acc_b", keep_account_id="acc_a")
        svc.submit_proof(session.session_id, valid_proof("id_a"), now=NOW)
        svc.submit_proof(session.session_id, valid_proof("id_b"), now=NOW)
        return session

    def test_concurrent_confirm_and_withdraw_single_outcome(self):
        for _ in range(20):  # 多轮交错调度，消除偶然性
            svc = make_service_with_two_accounts()
            session = self._prepared_session(svc)
            barrier = threading.Barrier(2)
            outcomes, errors = [], []

            def do_confirm():
                barrier.wait()
                try:
                    outcomes.append(svc.confirm(session.session_id, now=NOW).status)
                except SessionFinalizedError as exc:
                    errors.append(exc)

            def do_withdraw():
                barrier.wait()
                try:
                    outcomes.append(
                        svc.withdraw(session.session_id, "id_b", now=NOW).status)
                except SessionFinalizedError as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=do_confirm),
                       threading.Thread(target=do_withdraw)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # 恰好一个生效、一个被拒；最终状态唯一
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(len(errors), 1)
            final = svc._sessions[session.session_id].status
            self.assertEqual(final, outcomes[0])
            self.assertIn(final, (SessionStatus.CONFIRMED, SessionStatus.WITHDRAWN))

    def test_confirm_after_withdraw_rejected(self):
        svc = make_service_with_two_accounts()
        session = self._prepared_session(svc)
        svc.withdraw(session.session_id, "id_a", now=NOW)
        with self.assertRaises(SessionFinalizedError):
            svc.confirm(session.session_id, now=NOW)
        # 撤回未产生任何合并：双方身份仍各自解析
        self.assertEqual(svc.resolve_login("id_a")[0], "acc_a")
        self.assertEqual(svc.resolve_login("id_b")[0], "acc_b")

    def test_withdraw_after_confirm_rejected_and_merge_stands(self):
        svc = make_service_with_two_accounts()
        session = self._prepared_session(svc)
        svc.confirm(session.session_id, now=NOW)
        with self.assertRaises(SessionFinalizedError):
            svc.withdraw(session.session_id, "id_b", now=NOW)
        self.assertEqual(svc.resolve_login("id_b")[0], "acc_a")

    def test_withdraw_by_outsider_rejected(self):
        svc = make_service_with_two_accounts()
        svc.register_account("acc_x", "x@example.com")
        svc.register_identity("id_x", "idp-x", "sub-x", "acc_x")
        session = self._prepared_session(svc)
        with self.assertRaises(ProofInvalidError):
            svc.withdraw(session.session_id, "id_x", now=NOW)


class ResolveLoginTest(unittest.TestCase):
    def test_unknown_identity_raises(self):
        svc = make_service_with_two_accounts()
        with self.assertRaises(KeyError):
            svc.resolve_login("no-such-identity")


if __name__ == "__main__":
    unittest.main()
