"""服务层综合测试：整项适配、禁止跨项凑字段、一次性消费与并发辨明赢家。"""

from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Iterable

from federated_login.errors import (
    AssertionAlreadyConsumedError,
    FederationError,
    SubjectConfirmationError,
)
from federated_login.models import (
    Assertion,
    AudienceRestriction,
    LoginRequest,
    SubjectConfirmation,
)
from federated_login.service import AssertionConsumerService

UTC = timezone.utc
T0 = datetime(2026, 11, 20, 8, 0, 0, tzinfo=UTC)
WINDOW_END = T0 + timedelta(hours=1)

RECIPIENT = "https://sp.example.com/acs"
OTHER_RECIPIENT = "https://other.example.net/acs"
REQUEST_ID = "req-100"
ASSERTION_ID = "assertion-full-1"


def make_confirmation(
    confirmation_id: str,
    recipient: str = RECIPIENT,
    in_response_to: str = REQUEST_ID,
    not_before: datetime = T0,
    not_on_or_after: datetime = WINDOW_END,
) -> SubjectConfirmation:
    return SubjectConfirmation(
        confirmation_id=confirmation_id,
        recipient=recipient,
        in_response_to=in_response_to,
        not_before=not_before,
        not_on_or_after=not_on_or_after,
    )


def make_assertion(
    confirmations: Iterable[SubjectConfirmation],
    groups: Iterable[AudienceRestriction] = (AudienceRestriction(("app-1",)),),
    assertion_id: str = ASSERTION_ID,
) -> Assertion:
    return Assertion(
        assertion_id=assertion_id,
        audience_restrictions=tuple(groups),
        subject_confirmations=tuple(confirmations),
    )


def make_request(
    recipient: str = RECIPIENT,
    in_response_to: str = REQUEST_ID,
    delivery_id: str | None = None,
) -> LoginRequest:
    return LoginRequest(
        audience="app-1",
        recipient=recipient,
        in_response_to=in_response_to,
        delivery_id=delivery_id,
    )


def make_service(now: datetime | None = None) -> AssertionConsumerService:
    fixed = now if now is not None else T0 + timedelta(minutes=30)
    return AssertionConsumerService(clock=lambda: fixed)


class OneWholeConfirmationTest(unittest.TestCase):
    """“至少有一整项适配”与“禁止从两项里凑齐字段”。"""

    def test_whole_item_matching_all_three_fields_is_accepted(self) -> None:
        assertion = make_assertion([make_confirmation("c1")])
        result = make_service().consume(
            assertion, make_request(delivery_id="d-ok")
        )
        self.assertEqual(result.matched_confirmation.confirmation_id, "c1")
        self.assertEqual(result.delivery_id, "d-ok")
        self.assertEqual(result.record.matched_confirmation_id, "c1")

    def test_mixing_fields_from_two_items_is_rejected(self) -> None:
        # c1 只对接收端，c2 只对请求编号；没有任何一整项同时满足两者。
        assertion = make_assertion(
            [
                make_confirmation(
                    "c1", recipient=RECIPIENT, in_response_to="other-request"
                ),
                make_confirmation(
                    "c2", recipient=OTHER_RECIPIENT, in_response_to=REQUEST_ID
                ),
            ]
        )
        service = make_service()
        with self.assertRaises(SubjectConfirmationError) as ctx:
            service.consume(assertion, make_request())
        # 错误原因应分别来自两个候选项，证明逐项评估而非跨项拼接。
        joined = " | ".join(ctx.exception.reasons)
        self.assertIn("c1", joined)
        self.assertIn("c2", joined)
        self.assertFalse(service.is_consumed(ASSERTION_ID))

    def test_wrong_recipient_does_not_consume_quota(self) -> None:
        assertion = make_assertion([make_confirmation("c1")])
        service = make_service()
        with self.assertRaises(SubjectConfirmationError):
            service.consume(
                assertion, make_request(recipient=OTHER_RECIPIENT, delivery_id="bad")
            )
        # 错误接收端的投递不占名额：同断言随后正确投递仍能成功一次。
        self.assertFalse(service.is_consumed(ASSERTION_ID))
        result = service.consume(
            assertion, make_request(delivery_id="good")
        )
        self.assertEqual(result.delivery_id, "good")
        record = service.consumption_record(ASSERTION_ID)
        self.assertIsNotNone(record)
        self.assertEqual(record.delivery_id, "good")
        self.assertEqual(record.recipient, RECIPIENT)

    def test_wrong_request_id_is_rejected(self) -> None:
        assertion = make_assertion([make_confirmation("c1")])
        service = make_service()
        with self.assertRaises(SubjectConfirmationError):
            service.consume(assertion, make_request(in_response_to="req-X"))
        self.assertFalse(service.is_consumed(ASSERTION_ID))

    def test_assertion_without_confirmation_candidates_is_rejected(self) -> None:
        assertion = make_assertion([])
        service = make_service()
        with self.assertRaises(SubjectConfirmationError):
            service.consume(assertion, make_request())
        self.assertFalse(service.is_consumed(ASSERTION_ID))

    def test_second_confirmation_with_different_window_still_whole_item(self) -> None:
        # c1 窗口已过，c2 在当前窗口内且字段全中：允许命中 c2，但不允许把
        # c1 的任何字段与 c2 混用（混用请求会因 c2 整体不适配而被拒）。
        assertion = make_assertion(
            [
                make_confirmation(
                    "c1",
                    not_before=T0 - timedelta(hours=2),
                    not_on_or_after=T0 - timedelta(hours=1),
                ),
                make_confirmation("c2"),
            ]
        )
        result = make_service().consume(assertion, make_request())
        self.assertEqual(result.matched_confirmation.confirmation_id, "c2")


class TimeWindowBoundaryTest(unittest.TestCase):
    """半开区间 [not_before, not_on_or_after) 的边界与交错场景。"""

    def test_request_exactly_at_not_before_is_accepted(self) -> None:
        assertion = make_assertion([make_confirmation("c1")])
        result = make_service(now=T0).consume(assertion, make_request())
        self.assertEqual(result.consumed_at, T0)

    def test_request_exactly_at_not_on_or_after_is_rejected(self) -> None:
        assertion = make_assertion([make_confirmation("c1")])
        service = make_service(now=WINDOW_END)
        with self.assertRaises(SubjectConfirmationError):
            service.consume(assertion, make_request())
        self.assertFalse(service.is_consumed(ASSERTION_ID))
        # 右边界失败同样不占名额：退回窗口内仍可消费。
        result = make_service(now=WINDOW_END - timedelta(seconds=1)).consume(
            assertion, make_request(delivery_id="in-window")
        )
        self.assertEqual(result.delivery_id, "in-window")

    def test_expired_then_replay_remains_blocked_after_real_consumption(self) -> None:
        assertion = make_assertion([make_confirmation("c1")])
        service = make_service(now=T0 + timedelta(minutes=30))
        first = service.consume(assertion, make_request(delivery_id="first"))
        self.assertEqual(first.delivery_id, "first")
        # 时间推进到窗口外：重复投递即便换时间也因“已消费”被拒，而非窗口原因。
        service2 = AssertionConsumerService(
            clock=lambda: WINDOW_END + timedelta(minutes=10)
        )
        # 同一服务内登记仍在；直接复用 service 验证状态优先于时间判断。
        with self.assertRaises(AssertionAlreadyConsumedError):
            service.consume(assertion, make_request(delivery_id="late"))
        self.assertIsNotNone(service.consumption_record(ASSERTION_ID))
        self.assertEqual(
            service.consumption_record(ASSERTION_ID).delivery_id, "first"
        )
        # 上面的 service2 是独立实例，不会共享消费登记（隔离性说明）。
        self.assertFalse(service2.is_consumed(ASSERTION_ID))


class OneTimeConsumptionAndConcurrencyTest(unittest.TestCase):
    """一次性消费、重复投递拒绝、并发辨明赢家。"""

    def test_replay_after_success_is_rejected(self) -> None:
        assertion = make_assertion([make_confirmation("c1")])
        service = make_service()
        service.consume(assertion, make_request(delivery_id="first"))
        with self.assertRaises(AssertionAlreadyConsumedError) as ctx:
            service.consume(assertion, make_request(delivery_id="second"))
        self.assertEqual(ctx.exception.consumed_by, "first")
        self.assertEqual(
            service.consumption_record(ASSERTION_ID).delivery_id, "first"
        )

    def test_failed_attempt_then_success_then_blocked_sequence(self) -> None:
        # 交错场景：错误接收端（失败，不占名额）→ 正确投递（成功）→ 再投递（拒绝）。
        assertion = make_assertion([make_confirmation("c1")])
        service = make_service()

        with self.assertRaises(SubjectConfirmationError):
            service.consume(
                assertion,
                make_request(recipient=OTHER_RECIPIENT, delivery_id="wrong-1"),
            )
        self.assertFalse(service.is_consumed(ASSERTION_ID))

        result = service.consume(
            assertion, make_request(delivery_id="winner")
        )
        self.assertEqual(result.delivery_id, "winner")

        with self.assertRaises(AssertionAlreadyConsumedError):
            service.consume(assertion, make_request(delivery_id="replay"))

        record = service.consumption_record(ASSERTION_ID)
        self.assertEqual(record.delivery_id, "winner")

    def test_concurrent_valid_deliveries_exactly_one_succeeds(self) -> None:
        assertion = make_assertion([make_confirmation("c1")])
        service = make_service()
        n = 16
        barrier = threading.Barrier(n)

        def attempt(index: int) -> str:
            barrier.wait()  # 尽量让所有线程同时进入 consume，制造交错
            try:
                result = service.consume(
                    assertion, make_request(delivery_id=f"d-{index:02d}")
                )
                return f"ok:{result.delivery_id}"
            except AssertionAlreadyConsumedError:
                return "replayed"
            except FederationError:  # pragma: no cover - 不应出现其他错误
                return "other-error"

        with ThreadPoolExecutor(max_workers=n) as pool:
            outcomes = list(pool.map(attempt, range(n)))

        ok_outcomes = [o for o in outcomes if o.startswith("ok:")]
        self.assertEqual(len(ok_outcomes), 1, outcomes)
        winner = ok_outcomes[0].split(":", 1)[1]
        self.assertEqual(outcomes.count("replayed"), n - 1)

        record = service.consumption_record(ASSERTION_ID)
        self.assertIsNotNone(record)
        self.assertEqual(record.delivery_id, winner)
        self.assertTrue(service.is_consumed(ASSERTION_ID))

    def test_concurrent_mix_of_bad_and_good_deliveries(self) -> None:
        # 并发混合：一半发往错误接收端（失败且不占名额），另一半合法。
        # 错误投递在消费完成前到达 -> SubjectConfirmationError；
        # 在消费完成后到达 -> 先命中已消费检查 -> AssertionAlreadyConsumedError。
        # 两类拒绝的划分随调度变化，但不变量是：赢家恰好一个且必为合法投递，
        # 错误投递永远不可能成为赢家，也不会挤掉合法投递的名额。
        assertion = make_assertion([make_confirmation("c1")])
        service = make_service()
        n_good, n_bad = 8, 8
        barrier = threading.Barrier(n_good + n_bad)

        def attempt(index: int) -> str:
            barrier.wait()
            bad = index >= n_good
            request = make_request(
                recipient=OTHER_RECIPIENT if bad else RECIPIENT,
                delivery_id=f"bad-{index}" if bad else f"good-{index}",
            )
            try:
                result = service.consume(assertion, request)
                return f"ok:{result.delivery_id}"
            except SubjectConfirmationError:
                return "bad-recipient"
            except AssertionAlreadyConsumedError:
                return "replayed"

        with ThreadPoolExecutor(max_workers=n_good + n_bad) as pool:
            outcomes = list(pool.map(attempt, range(n_good + n_bad)))

        ok_outcomes = [o for o in outcomes if o.startswith("ok:")]
        self.assertEqual(len(ok_outcomes), 1, outcomes)
        winner = ok_outcomes[0].split(":", 1)[1]
        self.assertTrue(winner.startswith("good-"), outcomes)
        # 除唯一赢家外，其余投递只能是这两类拒绝，不允许出现其他结果。
        self.assertEqual(
            outcomes.count("bad-recipient") + outcomes.count("replayed"),
            n_good + n_bad - 1,
        )
        self.assertLessEqual(outcomes.count("bad-recipient"), n_bad)
        record = service.consumption_record(ASSERTION_ID)
        self.assertEqual(record.delivery_id, winner)

    def test_distinct_assertions_have_independent_consumption_slots(self) -> None:
        a1 = make_assertion([make_confirmation("c1")], assertion_id="assert-A")
        a2 = make_assertion([make_confirmation("c1")], assertion_id="assert-B")
        service = make_service()
        r1 = service.consume(a1, make_request(delivery_id="for-A"))
        r2 = service.consume(a2, make_request(delivery_id="for-B"))
        self.assertEqual(r1.assertion_id, "assert-A")
        self.assertEqual(r2.assertion_id, "assert-B")
        self.assertTrue(service.is_consumed("assert-A"))
        self.assertTrue(service.is_consumed("assert-B"))


if __name__ == "__main__":
    unittest.main()
