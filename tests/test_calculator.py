"""核算器门面、全天扫描构造与交错边界测试。"""

import unittest

from sla_accounting import (
    OfflineBudgetCalculator,
    build_day_sweep,
)
from sla_accounting.accounting import ViolationStatus
from sla_accounting.aggregation import ObservationReport
from sla_accounting.calculator import ServiceResult
from sla_accounting.clocks import ClockSource
from sla_accounting.model import (
    Attempt,
    Contract,
    LogicalRequest,
    Outcome,
    WindowSpec,
)
from sla_accounting.windows import WindowStatus

CLK_A = ClockSource("a", epsilon=0.2)
CLK_B = ClockSource("b", epsilon=0.5)


def att(clock, region, observer, start, finish, outcome):
    return Attempt(
        region=region, observer=observer,
        start=clock.observe(start), finish=clock.observe(finish),
        outcome=outcome, deadline=clock.observe(start + 2.0),
    )


class BuildDaySweepTest(unittest.TestCase):
    def test_ticks_include_horizon_end(self) -> None:
        times = build_day_sweep((0.0, 100.0), tick_seconds=30.0)
        self.assertEqual(times[0], 30.0)
        self.assertEqual(times[-1], 100.0)
        # 步长严格为 tick，末尾由 horizon 右端点补齐
        self.assertTrue(all(b - a == 30.0 for a, b in zip(times, times[1:-1])))

    def test_single_tick_when_horizon_shorter_than_tick(self) -> None:
        self.assertEqual(build_day_sweep((0.0, 10.0), 60.0), (10.0,))

    def test_invalid_arguments(self) -> None:
        with self.assertRaises(ValueError):
            build_day_sweep((10.0, 10.0), 1.0)
        with self.assertRaises(ValueError):
            build_day_sweep((0.0, 10.0), 0.0)


class CalculatorFacadeTest(unittest.TestCase):
    def _contract(self) -> Contract:
        return Contract(
            service="svc",
            windows=(
                WindowSpec("short", 100.0, 1),
                WindowSpec("long", 1000.0, 5),
            ),
        )

    def test_full_pipeline_returns_service_result(self) -> None:
        req = LogicalRequest(
            request_id="q", service="svc", origin_region="region-a",
            attempts=(att(CLK_A, "region-a", "obs-a", 0.0, 3.0, Outcome.TIMEOUT),),
        )
        report = ObservationReport(
            observer="obs-a", region="region-a",
            covered_intervals=((0.0, 100.0),), requests=(req,),
        )
        calc = OfflineBudgetCalculator(self._contract(), horizon=(0.0, 100.0))
        result = calc.evaluate([report], eval_times=(50.0, 100.0))
        self.assertIsInstance(result, ServiceResult)
        self.assertEqual(len(result.window_timeseries), 2)
        self.assertEqual(
            result.outcome_at("short", 50.0).status, WindowStatus.EXHAUSTED
        )

    def test_duplicate_window_name_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Contract(
                service="svc",
                windows=(WindowSpec("w", 100.0, 1), WindowSpec("w", 200.0, 2)),
            )

    def test_zero_budget_any_violation_exhausts(self) -> None:
        contract = Contract(
            service="svc", windows=(WindowSpec("zero", 100.0, 0),),
        )
        req = LogicalRequest(
            request_id="q", service="svc", origin_region="region-a",
            attempts=(att(CLK_A, "region-a", "obs-a", 0.0, 3.0, Outcome.TIMEOUT),),
        )
        report = ObservationReport(
            observer="obs-a", region="region-a",
            covered_intervals=((0.0, 100.0),), requests=(req,),
        )
        result = OfflineBudgetCalculator(contract, (0.0, 100.0)).evaluate(
            [report], (50.0,)
        )
        outcome = result.outcome_at("zero", 50.0)
        self.assertEqual(outcome.status, WindowStatus.EXHAUSTED)
        self.assertEqual(outcome.remaining_best, -1)


class InterleaveTest(unittest.TestCase):
    """交错情况：两个观察者覆盖区间交错、交接链跨时钟源。"""

    def test_interleaved_coverage_unifies_with_two_gaps(self) -> None:
        report_a = ObservationReport(
            observer="obs-a", region="region-a",
            covered_intervals=((0.0, 20.0), (60.0, 80.0)), requests=(),
        )
        report_b = ObservationReport(
            observer="obs-b", region="region-b",
            covered_intervals=((30.0, 50.0), (75.0, 100.0)), requests=(),
        )
        calc = OfflineBudgetCalculator(
            Contract("svc", (WindowSpec("w", 100.0, 1),)),
            horizon=(0.0, 100.0),
        )
        result = calc.evaluate([report_a, report_b], (100.0,))
        self.assertEqual(
            result.aggregate.coverage,
            ((0.0, 20.0), (30.0, 50.0), (60.0, 100.0)),
        )
        self.assertEqual(result.aggregate.gaps, ((20.0, 30.0), (50.0, 60.0)))

    def test_cross_clock_handoff_chain_uses_combined_error_band(self) -> None:
        # 首跳 a 时钟（ε=0.2），重试跳 b 时钟（ε=0.5）
        req = LogicalRequest(
            request_id="h",
            service="svc",
            origin_region="region-a",
            handed_off=True,
            attempts=(
                att(CLK_A, "region-a", "obs-a", 0.0, 0.5, Outcome.FAILURE),
                # b: 起点 10，截止读数 12；结算 12.2，误差带相交 -> 可能
                att(CLK_B, "region-b", "obs-b", 10.0, 12.2, Outcome.SUCCESS),
            ),
        )
        report = ObservationReport(
            observer="obs-a", region="region-a",
            covered_intervals=((0.0, 100.0),), requests=(req,),
        )
        calc = OfflineBudgetCalculator(
            Contract("svc", (WindowSpec("w", 100.0, 1),)), (0.0, 100.0)
        )
        result = calc.evaluate([report], (50.0,))
        verdict = result.aggregate.verdict_of("h")
        self.assertEqual(verdict.status, ViolationStatus.POSSIBLE)
        outcome = result.outcome_at("w", 50.0)
        self.assertEqual(outcome.possible_violations, 1)
        self.assertEqual(outcome.status, WindowStatus.AT_RISK)

    def test_interleaved_retry_chain_all_violations_attach_to_one_request(self) -> None:
        # 三跳跨两区，两个延迟违约 + 一个可用性失败，预算事件仍只算 1 条
        req = LogicalRequest(
            request_id="chain",
            service="svc",
            origin_region="region-a",
            handed_off=True,
            attempts=(
                att(CLK_A, "region-a", "obs-a", 0.0, 0.5, Outcome.FAILURE),
                att(CLK_B, "region-b", "obs-b", 10.0, 14.0, Outcome.FAILURE),
                att(CLK_B, "region-b", "obs-b", 20.0, 24.0, Outcome.SUCCESS),
            ),
        )
        report = ObservationReport(
            observer="obs-a", region="region-a",
            covered_intervals=((0.0, 100.0),), requests=(req,),
        )
        calc = OfflineBudgetCalculator(
            Contract("svc", (WindowSpec("w", 100.0, 5),)), (0.0, 100.0)
        )
        result = calc.evaluate([report], (50.0,))
        verdict = result.aggregate.verdict_of("chain")
        # 第 2 跳：可用性 + 延迟；第 3 跳：延迟；首跳豁免
        self.assertEqual(verdict.definite_violations, 3)
        outcome = result.outcome_at("w", 50.0)
        self.assertEqual(outcome.definite_violations, 1)  # 只消耗 1 个事件额度


if __name__ == "__main__":
    unittest.main()
