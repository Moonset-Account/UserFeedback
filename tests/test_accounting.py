"""单条逻辑请求核算测试：首跳豁免、超期重试不可消除、最终失败不豁免。"""

import unittest

from sla_accounting.accounting import (
    RequestVerdict,
    ViolationStatus,
    evaluate_request,
)
from sla_accounting.clocks import ClockSource, Ternary
from sla_accounting.model import (
    Attempt,
    LogicalRequest,
    Outcome,
)

CLK = ClockSource("test-clk", epsilon=0.1)


def attempt(
    start: float,
    finish: float,
    outcome: Outcome,
    region: str = "region-a",
    observer: str = "obs-a",
    clock: ClockSource = CLK,
    deadline: float | None = None,
) -> Attempt:
    """构造一跳；截止时间默认 = 起点 + 2.0。"""
    return Attempt(
        region=region,
        observer=observer,
        start=clock.observe(start),
        finish=clock.observe(finish),
        outcome=outcome,
        deadline=clock.observe(start + 2.0 if deadline is None else deadline),
    )


def request(
    request_id: str,
    attempts: tuple[Attempt, ...],
    handed_off: bool = False,
) -> LogicalRequest:
    regions = {a.region for a in attempts}
    return LogicalRequest(
        request_id=request_id,
        service="svc",
        origin_region="region-a",
        attempts=attempts,
        handed_off=handed_off or len(regions) > 1,
    )


class FirstHopExcuseTest(unittest.TestCase):
    def test_final_success_forgives_first_hop_failure(self) -> None:
        # 首跳失败（且守约），第二跳成功 -> 不耗预算
        verdict = evaluate_request(
            request(
                "q1",
                (
                    attempt(0.0, 0.5, Outcome.FAILURE),
                    attempt(1.0, 1.5, Outcome.SUCCESS),
                ),
            )
        )
        self.assertEqual(verdict.status, ViolationStatus.NONE)
        self.assertEqual(verdict.excused_availability, 1)
        self.assertTrue(verdict.marks[0].excused)
        self.assertFalse(verdict.marks[1].excused)
        self.assertFalse(verdict.consumes_budget)

    def test_final_success_forgives_first_hop_timeout(self) -> None:
        # 首跳超时（可用性 + 超截止），第二跳成功 -> 全部豁免
        verdict = evaluate_request(
            request(
                "q2",
                (
                    attempt(0.0, 3.0, Outcome.TIMEOUT),
                    attempt(4.0, 4.5, Outcome.SUCCESS),
                ),
            )
        )
        self.assertEqual(verdict.status, ViolationStatus.NONE)
        self.assertEqual(verdict.excused_availability, 1)
        self.assertEqual(verdict.excused_latency, 1)

    def test_final_failure_does_not_forgive_first_hop(self) -> None:
        verdict = evaluate_request(
            request(
                "q3",
                (
                    attempt(0.0, 0.5, Outcome.FAILURE),
                    attempt(1.0, 1.5, Outcome.FAILURE),
                ),
            )
        )
        self.assertEqual(verdict.status, ViolationStatus.DEFINITE)
        self.assertEqual(verdict.excused_availability, 0)
        self.assertEqual(verdict.definite_violations, 2)
        self.assertTrue(verdict.consumes_budget)

    def test_single_hop_success_is_clean(self) -> None:
        verdict = evaluate_request(
            request("q4", (attempt(0.0, 0.4, Outcome.SUCCESS),))
        )
        self.assertEqual(verdict.status, ViolationStatus.NONE)


class RetryLatencyTest(unittest.TestCase):
    def test_late_retry_violation_survives_final_success(self) -> None:
        # 题面核心：超出截止时间的重试不能消除已经发生的违约。
        # 首跳失败被豁免；第二跳成功但结算 4.0 > 截止 3.0（确定超时）。
        verdict = evaluate_request(
            request(
                "q5",
                (
                    attempt(0.0, 0.5, Outcome.FAILURE),
                    attempt(1.0, 4.0, Outcome.SUCCESS),
                ),
            )
        )
        self.assertEqual(verdict.status, ViolationStatus.DEFINITE)
        self.assertEqual(verdict.excused_availability, 1)
        self.assertEqual(verdict.definite_violations, 1)
        self.assertEqual(verdict.marks[1].latency_violation, ViolationStatus.DEFINITE)

    def test_retry_availability_failure_counts_even_when_later_success(self) -> None:
        # 第二跳可用性失败，第三跳成功：第二跳不享受首跳豁免
        verdict = evaluate_request(
            request(
                "q6",
                (
                    attempt(0.0, 0.5, Outcome.FAILURE),
                    attempt(1.0, 1.5, Outcome.FAILURE),
                    attempt(2.0, 2.5, Outcome.SUCCESS),
                ),
            )
        )
        self.assertEqual(verdict.status, ViolationStatus.DEFINITE)
        self.assertEqual(verdict.definite_violations, 1)
        self.assertEqual(verdict.excused_availability, 1)

    def test_multiple_late_retries_count_individually_in_detail(self) -> None:
        # 明细口径逐条计数，但预算事件只算 1 条
        verdict = evaluate_request(
            request(
                "q7",
                (
                    attempt(0.0, 0.5, Outcome.FAILURE),
                    attempt(1.0, 4.0, Outcome.FAILURE),   # 超截止
                    attempt(5.0, 9.0, Outcome.SUCCESS),   # 超截止
                ),
            )
        )
        self.assertEqual(verdict.status, ViolationStatus.DEFINITE)
        # 第二跳：可用性 1 + 延迟 1；第三跳：延迟 1
        self.assertEqual(verdict.definite_violations, 3)


class ClockUncertaintyTest(unittest.TestCase):
    def test_uncertain_deadline_gives_possible_violation(self) -> None:
        loose = ClockSource("loose", epsilon=0.5)
        # 起点 10，截止读数 12 -> 区间 [11.5, 12.5]
        # 结算读数 12.2 -> [11.7, 12.7]，相交 -> UNCERTAIN
        q = request(
            "q8",
            (
                attempt(0.0, 0.5, Outcome.FAILURE),
                attempt(10.0, 12.2, Outcome.SUCCESS, clock=loose),
            ),
        )
        verdict = evaluate_request(q)
        self.assertEqual(verdict.status, ViolationStatus.POSSIBLE)
        self.assertEqual(verdict.possible_violations, 1)
        self.assertEqual(verdict.definite_violations, 0)
        self.assertIs(
            verdict.marks[1].deadline_verdict, Ternary.UNCERTAIN
        )

    def test_definite_violation_dominates_possible(self) -> None:
        loose = ClockSource("loose", epsilon=0.5)
        q = request(
            "q9",
            (
                attempt(0.0, 0.5, Outcome.FAILURE),
                attempt(10.0, 12.2, Outcome.FAILURE, clock=loose),  # 可能延迟 + 确定可用性
                attempt(20.0, 20.5, Outcome.SUCCESS),
            ),
        )
        verdict = evaluate_request(q)
        self.assertEqual(verdict.status, ViolationStatus.DEFINITE)

    def test_uncertain_first_hop_excused_when_succeeds(self) -> None:
        loose = ClockSource("loose", epsilon=0.5)
        # 首跳就在误差带内，但最终成功 -> 首跳可能违约一并豁免
        q = request(
            "q10",
            (
                attempt(0.0, 2.2, Outcome.FAILURE, clock=loose),
                attempt(5.0, 5.5, Outcome.SUCCESS),
            ),
        )
        verdict = evaluate_request(q)
        self.assertEqual(verdict.status, ViolationStatus.NONE)
        self.assertEqual(verdict.excused_latency, 1)


class ModelValidationTest(unittest.TestCase):
    def test_empty_attempts_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LogicalRequest(
                request_id="x",
                service="svc",
                origin_region="region-a",
                attempts=(),
            )

    def test_handed_off_requires_two_regions(self) -> None:
        with self.assertRaises(ValueError):
            LogicalRequest(
                request_id="x",
                service="svc",
                origin_region="region-a",
                attempts=(attempt(0.0, 0.5, Outcome.SUCCESS),),
                handed_off=True,
            )

    def test_origin_region_must_appear_in_chain(self) -> None:
        with self.assertRaises(ValueError):
            LogicalRequest(
                request_id="x",
                service="svc",
                origin_region="region-z",
                attempts=(attempt(0.0, 0.5, Outcome.SUCCESS),),
            )


if __name__ == "__main__":
    unittest.main()
