"""受众限制交叉约束测试：组内 OR、组间 AND。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from federated_login.errors import AudienceRestrictionError
from federated_login.models import (
    Assertion,
    AudienceRestriction,
    LoginRequest,
    SubjectConfirmation,
)
from federated_login.service import AssertionConsumerService

UTC = timezone.utc
T0 = datetime(2026, 11, 20, 8, 0, 0, tzinfo=UTC)


def make_request(audience: str = "app-1") -> LoginRequest:
    return LoginRequest(
        audience=audience,
        recipient="https://sp.example.com/acs",
        in_response_to="req-100",
        delivery_id=f"delivery-{audience}",
    )


def make_assertion(
    groups: tuple[AudienceRestriction, ...],
) -> Assertion:
    return Assertion(
        assertion_id="assertion-aud-1",
        audience_restrictions=groups,
        subject_confirmations=(
            SubjectConfirmation(
                confirmation_id="c1",
                recipient="https://sp.example.com/acs",
                in_response_to="req-100",
                not_before=T0,
                not_on_or_after=T0 + timedelta(hours=1),
            ),
        ),
    )


def make_service() -> AssertionConsumerService:
    fixed_now = T0 + timedelta(minutes=30)
    return AssertionConsumerService(clock=lambda: fixed_now)


class AudienceRestrictionVerificationTest(unittest.TestCase):
    def test_every_group_hit_by_one_audience_is_accepted(self) -> None:
        # 两个限制组；app-1 在每组各命中一个条目（组内 OR，组间 AND）。
        assertion = make_assertion(
            (
                AudienceRestriction(("app-1", "app-2")),
                AudienceRestriction(("app-1", "app-3")),
            )
        )
        result = make_service().consume(assertion, make_request("app-1"))
        self.assertEqual(result.assertion_id, "assertion-aud-1")
        self.assertEqual(result.audience, "app-1")

    def test_missing_first_group_is_rejected_with_group_index(self) -> None:
        assertion = make_assertion(
            (
                AudienceRestriction(("app-2",)),  # app-1 不在第一组
                AudienceRestriction(("app-1",)),
            )
        )
        service = make_service()
        with self.assertRaises(AudienceRestrictionError) as ctx:
            service.consume(assertion, make_request("app-1"))
        self.assertEqual(ctx.exception.failed_group_index, 0)
        self.assertFalse(service.is_consumed("assertion-aud-1"))

    def test_missing_second_group_is_rejected_with_group_index(self) -> None:
        # 两组的公共受众是 app-3：app-1 只过第一组，app-3 两组都过。
        assertion = make_assertion(
            (
                AudienceRestriction(("app-1", "app-3")),
                AudienceRestriction(("app-2", "app-3")),  # app-1 在第二组未命中
            )
        )
        service = make_service()
        with self.assertRaises(AudienceRestrictionError) as ctx:
            service.consume(assertion, make_request("app-1"))
        self.assertEqual(ctx.exception.failed_group_index, 1)
        # 被拒绝的请求不占消费名额：随后换成满足两组的受众仍可成功。
        result = service.consume(assertion, make_request("app-3"))
        self.assertEqual(result.audience, "app-3")
        self.assertTrue(service.is_consumed("assertion-aud-1"))

    def test_assertion_without_any_group_is_rejected(self) -> None:
        assertion = make_assertion(())
        service = make_service()
        with self.assertRaises(AudienceRestrictionError):
            service.consume(assertion, make_request())
        self.assertFalse(service.is_consumed("assertion-aud-1"))

    def test_unknown_audience_is_rejected(self) -> None:
        assertion = make_assertion((AudienceRestriction(("app-1",)),))
        service = make_service()
        with self.assertRaises(AudienceRestrictionError):
            service.consume(assertion, make_request("app-999"))
        self.assertFalse(service.is_consumed("assertion-aud-1"))


if __name__ == "__main__":
    unittest.main()
