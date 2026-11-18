"""恢复会话模块的验收测试。

覆盖题面的全部交叉约束：

* 可接受结果：冷静期 + 旧设备确认后，用一次性恢复码完成指定因素替换；
* 应被阻止/拒绝的结果：冷静期内提交、未确认旧设备、错误恢复码、撤回后
  提交、替换非申请指定因素、会话过期、恢复成功后再次提交；
* 边界/交错场景：冷静期临界点、有效期临界点、同请求编号重试取回原决定
  （含“先拒绝后条件满足仍回放原拒绝”）、多线程并发提交只成功一次。
"""

from __future__ import annotations

import threading
import unittest

from recovery import (
    REASON_CODE_ALREADY_USED,
    REASON_CODE_MISMATCH,
    REASON_COOLDOWN,
    REASON_EXPIRED,
    REASON_FINAL_STATE,
    REASON_NOT_CONFIRMED,
    REASON_TARGET_MISMATCH,
    REASON_WITHDRAWN,
    EventType,
    Factor,
    FactorRegistry,
    ManualClock,
    RecoveryManager,
    SessionStatus,
    UnknownSessionError,
)

COOLDOWN = 10.0
VALIDITY = 100.0
USER = "user-1"
TARGET = "factor-totp"
OTHER = "factor-sms"
CODE = "1234567890-ABCDEF"


class RecoveryFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.registry = FactorRegistry()
        self.registry.add_factor(
            USER, Factor(TARGET, "totp", "旧验证器", secret="old-secret")
        )
        self.registry.add_factor(
            USER, Factor(OTHER, "sms", "短信因素", secret="+10000")
        )
        self.manager = RecoveryManager(self.registry, clock=self.clock)
        self.session = self.manager.create_application(
            USER,
            TARGET,
            CODE,
            cooldown_seconds=COOLDOWN,
            validity_seconds=VALIDITY,
        )

    def _replacement(self, factor_id: str = TARGET) -> Factor:
        return Factor(factor_id, "webauthn", "新通行密钥", secret="new-secret")

    def _ready(self) -> None:
        """推进到冷静期结束并完成旧设备确认。"""
        self.session.confirm_old_device()
        self.clock.advance(COOLDOWN)

    # ------------------------------------------------------------------ #
    # 可接受结果
    # ------------------------------------------------------------------ #
    def test_happy_path_replaces_only_target_factor(self) -> None:
        self._ready()
        decision = self.session.submit_recovery("req-1", CODE, self._replacement())

        self.assertTrue(decision.accepted)
        self.assertIsNone(decision.reason)
        self.assertEqual(decision.session_id, self.session.session_id)
        self.assertEqual(self.session.status, SessionStatus.RECOVERED)
        self.assertTrue(self.session.recovery_code_used)

        factors = {f.factor_id: f for f in self.registry.list_factors(USER)}
        # 指定因素被替换：新因素启用，旧因素停用但记录保留。
        self.assertTrue(factors[TARGET].enabled)
        self.assertEqual(factors[TARGET].factor_type, "webauthn")
        self.assertEqual(factors[TARGET].secret, "new-secret")
        # 非申请指定的因素完全不受影响，继续可用。
        self.assertTrue(factors[OTHER].enabled)
        self.assertEqual(factors[OTHER].factor_type, "sms")
        self.assertEqual(self.registry.enabled_factor_ids(USER), [OTHER, TARGET])

    def test_cooldown_boundary_just_before_and_exactly_at(self) -> None:
        self.session.confirm_old_device()

        # 边界内侧（差 1 个时间单位）：仍在冷静期，拒绝。
        self.clock.advance(COOLDOWN - 1)
        early = self.session.submit_recovery("req-early", CODE, self._replacement())
        self.assertFalse(early.accepted)
        self.assertEqual(early.reason, REASON_COOLDOWN)
        self.assertEqual(self.session.status, SessionStatus.PENDING)

        # 临界点（恰好满 cooldown_seconds）：放行并成功。
        self.clock.advance(1)
        ok = self.session.submit_recovery("req-ok", CODE, self._replacement())
        self.assertTrue(ok.accepted)
        self.assertEqual(self.session.status, SessionStatus.RECOVERED)

    # ------------------------------------------------------------------ #
    # 应被阻止 / 拒绝的结果
    # ------------------------------------------------------------------ #
    def test_submit_during_cooldown_is_rejected(self) -> None:
        self.session.confirm_old_device()
        self.clock.advance(COOLDOWN / 2)

        decision = self.session.submit_recovery("req-c", CODE, self._replacement())
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, REASON_COOLDOWN)
        # 被拒绝不消耗恢复码，也不改动任何因素。
        self.assertFalse(self.session.recovery_code_used)
        self.assertEqual(
            self.registry.enabled_factor_ids(USER), [OTHER, TARGET]
        )
        self.assertEqual(self.session.status, SessionStatus.PENDING)

    def test_submit_without_old_device_confirmation_is_rejected(self) -> None:
        self.clock.advance(COOLDOWN)
        decision = self.session.submit_recovery("req-nc", CODE, self._replacement())
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, REASON_NOT_CONFIRMED)
        self.assertFalse(self.session.recovery_code_used)

    def test_wrong_recovery_code_is_rejected_and_not_consumed(self) -> None:
        self._ready()
        decision = self.session.submit_recovery(
            "req-bad", "WRONG-CODE", self._replacement()
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, REASON_CODE_MISMATCH)
        self.assertFalse(self.session.recovery_code_used)

        # 恢复码未被消耗：同一请求编号之外的新请求仍可用正确码成功。
        ok = self.session.submit_recovery("req-good", CODE, self._replacement())
        self.assertTrue(ok.accepted)

    def test_withdrawal_before_reenrollment_voids_session_and_keeps_factors(self) -> None:
        self._ready()
        event = self.session.withdraw()
        self.assertEqual(event.event_type, EventType.WITHDRAWN)
        self.assertEqual(self.session.status, SessionStatus.WITHDRAWN)

        before = self.registry.enabled_factor_ids(USER)
        decision = self.session.submit_recovery("req-w", CODE, self._replacement())
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, REASON_WITHDRAWN)
        # 撤回不触碰登记簿，原有因素继续可用。
        self.assertEqual(self.registry.enabled_factor_ids(USER), before)
        self.assertFalse(self.session.recovery_code_used)

        # 撤回是终态，旧设备确认也必须被阻止。
        with self.assertRaises(Exception):
            self.session.confirm_old_device()

    def test_cannot_replace_factor_other_than_application_target(self) -> None:
        self._ready()
        decision = self.session.submit_recovery(
            "req-target", CODE, self._replacement(factor_id=OTHER)
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, REASON_TARGET_MISMATCH)
        self.assertFalse(self.session.recovery_code_used)
        # 两个因素都保持原状。
        self.assertEqual(
            self.registry.enabled_factor_ids(USER), [OTHER, TARGET]
        )

    def test_expired_session_rejects_submission_at_validity_boundary(self) -> None:
        self.session.confirm_old_device()

        # 有效期临界点内仍可成功。
        self.clock.advance(VALIDITY)
        ok = self.session.submit_recovery("req-edge", CODE, self._replacement())
        self.assertTrue(ok.accepted)

        # 新会话：越过有效期一秒即过期。
        session2 = self.manager.create_application(
            USER, TARGET, CODE, COOLDOWN, VALIDITY
        )
        session2.confirm_old_device()
        self.clock.advance(VALIDITY + 1)
        expired = session2.submit_recovery(
            "req-exp", CODE, self._replacement()
        )
        self.assertFalse(expired.accepted)
        self.assertEqual(expired.reason, REASON_EXPIRED)
        self.assertEqual(session2.status, SessionStatus.EXPIRED)
        self.assertEqual(
            [e.event_type for e in session2.events_of(EventType.EXPIRED)],
            [EventType.EXPIRED],
        )

    def test_second_submit_after_success_is_rejected(self) -> None:
        self._ready()
        first = self.session.submit_recovery("req-1", CODE, self._replacement())
        self.assertTrue(first.accepted)

        second = self.session.submit_recovery("req-2", CODE, self._replacement())
        self.assertFalse(second.accepted)
        self.assertEqual(second.reason, REASON_FINAL_STATE)
        # 恢复码使用与重新登记事件都只出现一次。
        self.assertEqual(
            len(self.session.events_of(EventType.RECOVERY_CODE_USED)), 1
        )
        self.assertEqual(len(self.session.events_of(EventType.REENROLLED)), 1)

    # ------------------------------------------------------------------ #
    # 幂等重试与并发交错
    # ------------------------------------------------------------------ #
    def test_same_request_id_retry_returns_original_success_decision(self) -> None:
        self._ready()
        first = self.session.submit_recovery("req-idem", CODE, self._replacement())
        second = self.session.submit_recovery("req-idem", CODE, self._replacement())

        self.assertIs(first, second)
        self.assertTrue(second.accepted)
        self.assertEqual(
            len(self.session.events_of(EventType.RECOVERY_CODE_USED)), 1
        )

    def test_replayed_rejection_is_pinned_even_after_condition_clears(self) -> None:
        # 冷静期内用 req-x 提交，被拒绝并缓存原决定。
        self.session.confirm_old_device()
        rejected = self.session.submit_recovery(
            "req-x", CODE, self._replacement()
        )
        self.assertEqual(rejected.reason, REASON_COOLDOWN)

        # 客户端超时后才重试，此时冷静期已过，但同请求编号必须回放原拒绝。
        self.clock.advance(COOLDOWN + 1)
        replayed = self.session.submit_recovery(
            "req-x", CODE, self._replacement()
        )
        self.assertIs(rejected, replayed)
        self.assertFalse(replayed.accepted)
        self.assertEqual(replayed.reason, REASON_COOLDOWN)
        self.assertFalse(self.session.recovery_code_used)

        # 换一个新请求编号则按当前状态正常处理（成功）。
        fresh = self.session.submit_recovery("req-y", CODE, self._replacement())
        self.assertTrue(fresh.accepted)

    def test_concurrent_different_request_ids_only_one_succeeds(self) -> None:
        self._ready()
        results: dict[str, object] = {}
        barrier = threading.Barrier(4)

        def worker(request_id: str) -> None:
            barrier.wait()
            results[request_id] = self.session.submit_recovery(
                request_id, CODE, self._replacement()
            )

        threads = [
            threading.Thread(target=worker, args=(f"req-{i}",))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        decisions = list(results.values())
        accepted = [d for d in decisions if d.accepted]
        rejected = [d for d in decisions if not d.accepted]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 3)
        self.assertTrue(
            all(d.reason in (REASON_FINAL_STATE, REASON_CODE_ALREADY_USED)
                for d in rejected)
        )
        self.assertEqual(self.session.status, SessionStatus.RECOVERED)
        self.assertEqual(
            len(self.session.events_of(EventType.REENROLLED)), 1
        )

    def test_concurrent_same_request_id_returns_single_shared_decision(self) -> None:
        self._ready()
        results: list[object] = []
        barrier = threading.Barrier(4)
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            decision = self.session.submit_recovery(
                "req-same", CODE, self._replacement()
            )
            with lock:
                results.append(decision)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 4)
        self.assertTrue(all(d is results[0] for d in results))
        self.assertTrue(results[0].accepted)
        self.assertEqual(
            len(self.session.events_of(EventType.RECOVERY_CODE_USED)), 1
        )

    # ------------------------------------------------------------------ #
    # 审计事件流与管理器
    # ------------------------------------------------------------------ #
    def test_event_stream_records_full_audit_trail(self) -> None:
        self.session.confirm_old_device()
        self.clock.advance(COOLDOWN)
        # 先制造一次拒绝，再成功，验证四类事实都有留痕。
        bad = self.session.submit_recovery(
            "req-bad", "WRONG", self._replacement()
        )
        self.assertFalse(bad.accepted)
        good = self.session.submit_recovery("req-ok", CODE, self._replacement())
        self.assertTrue(good.accepted)

        types = [e.event_type for e in self.session.events]
        self.assertEqual(
            types,
            [
                EventType.CREATED,
                EventType.OLD_DEVICE_CONFIRMED,
                EventType.REENROLLMENT_REJECTED,
                EventType.RECOVERY_CODE_USED,
                EventType.REENROLLED,
            ],
        )
        # 事件序号连续且时间戳单调不减。
        self.assertEqual([e.seq for e in self.session.events], [1, 2, 3, 4, 5])
        self.assertTrue(
            all(
                self.session.events[i].timestamp
                <= self.session.events[i + 1].timestamp
                for i in range(len(self.session.events) - 1)
            )
        )

    def test_withdrawal_event_is_recorded_and_idempotent(self) -> None:
        first = self.session.withdraw(reason="user_changed_mind")
        second = self.session.withdraw(reason="user_changed_mind")
        self.assertIs(first, second)
        withdrawals = self.session.events_of(EventType.WITHDRAWN)
        self.assertEqual(len(withdrawals), 1)
        self.assertEqual(withdrawals[0].detail["reason"], "user_changed_mind")

    def test_manager_lookup_and_validation(self) -> None:
        with self.assertRaises(UnknownSessionError):
            self.manager.get_session("does-not-exist")

        with self.assertRaises(ValueError):
            self.manager.create_application(
                USER, TARGET, CODE,
                cooldown_seconds=10, validity_seconds=5,
            )
        with self.assertRaises(LookupError):
            self.manager.create_application(
                USER, "missing-factor", CODE, COOLDOWN, VALIDITY
            )

        listed = self.manager.list_sessions(USER)
        self.assertEqual([s.session_id for s in listed], [self.session.session_id])
        self.assertEqual(self.manager.list_sessions("nobody"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
