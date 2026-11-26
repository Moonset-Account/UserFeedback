"""滑动窗口预算核算测试：三态边界、剩余额度、窗口状态与滑出。"""

import unittest

from sla_accounting.aggregation import ObservationReport, merge_reports
from sla_accounting.clocks import ClockSource
from sla_accounting.model import (
    Attempt,
    LogicalRequest,
    Outcome,
    WindowSpec,
)
from sla_accounting.windows import (
    WindowStatus,
    evaluate_window,
    summarize,
    sweep_windows,
)

CLK = ClockSource("w-clk", epsilon=0.0)  # 精确时钟，窗口边界可确定


def make_request(request_id: str, t: float, outcome: Outcome) -> LogicalRequest:
    return LogicalRequest(
        request_id=request_id,
        service="svc",
        origin_region="region-a",
        attempts=(
            Attempt(
                region="region-a",
                observer="obs-a",
                start=CLK.observe(t),
                finish=CLK.observe(t + (3.0 if outcome is not Outcome.SUCCESS else 0.5)),
                outcome=outcome,
                deadline=CLK.observe(t + 2.0),
            ),
        ),
    )


def build(verdicts_requests):
    requests = tuple(r for _, r in verdicts_requests)
    verdicts = tuple(v for v, _ in verdicts_requests)
    return requests, verdicts


class WindowStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        # 窗口 [at-100, at]，预算 2
        self.spec = WindowSpec("w", 100.0, 2)
        self.requests = tuple(
            make_request(f"q{i}", t, Outcome.TIMEOUT)
            for i, t in enumerate((10.0, 20.0, 30.0))
        )
        reports = [
            ObservationReport(
                observer="obs-a",
                region="region-a",
                covered_intervals=((0.0, 200.0),),
                requests=self.requests,
            )
        ]
        self.agg = merge_reports("svc", reports)
        self.by_id = {r.request_id: r for r in self.requests}

    def test_ok_when_worst_case_within_budget(self) -> None:
        # 窗口 [0,100] 内 3 条确定违约，但预算 2 -> 耗尽；先测预算 5 的口径
        spec = WindowSpec("big", 100.0, 5)
        outcome = evaluate_window(
            spec, 100.0, self.agg.verdicts, self.by_id
        )
        self.assertEqual(outcome.status, WindowStatus.OK)
        self.assertEqual(outcome.remaining_best, 2)
        self.assertEqual(outcome.remaining_worst, 2)

    def test_exhausted_when_definite_reaches_budget(self) -> None:
        outcome = evaluate_window(
            self.spec, 100.0, self.agg.verdicts, self.by_id
        )
        self.assertEqual(outcome.definite_violations, 3)
        self.assertEqual(outcome.status, WindowStatus.EXHAUSTED)
        self.assertEqual(outcome.remaining_best, -1)
        self.assertEqual(outcome.remaining_worst, -1)

    def test_events_slide_out_of_window(self) -> None:
        # 事件结算时刻分别为 13、23、33；窗口 [at-100, at]
        outcome = evaluate_window(
            self.spec, 120.0, self.agg.verdicts, self.by_id
        )
        # 窗口 [20,120]：结算 13 已滑出，剩 2 条
        self.assertEqual(outcome.definite_violations, 2)
        self.assertEqual(outcome.status, WindowStatus.EXHAUSTED)
        # 窗口 [34,134]：结算 33 也滑出 -> 0 条
        outcome_later = evaluate_window(
            self.spec, 134.0, self.agg.verdicts, self.by_id
        )
        self.assertEqual(outcome_later.definite_violations, 0)
        self.assertEqual(outcome_later.status, WindowStatus.OK)

    def test_closed_window_boundary_includes_endpoint_event(self) -> None:
        # q1 结算时刻 23 恰为窗口 [23,123] 左端点，闭区间应包含
        outcome = evaluate_window(
            self.spec, 123.0, self.agg.verdicts, self.by_id
        )
        self.assertIn("q1", outcome.request_ids)


class UncertainBudgetTest(unittest.TestCase):
    def test_at_risk_when_possible_consumes_remaining_budget(self) -> None:
        loose = ClockSource("loose", epsilon=0.5)

        def uncertain_request(rid: str, t: float) -> LogicalRequest:
            # 首跳失败（最终成功时豁免），第二跳成功但落入截止误差带，
            # 重试跳不享受豁免 -> 可能违约
            return LogicalRequest(
                request_id=rid,
                service="svc",
                origin_region="region-a",
                handed_off=True,
                attempts=(
                    Attempt(
                        region="region-a",
                        observer="obs-a",
                        start=CLK.observe(t),
                        finish=CLK.observe(t + 0.5),
                        outcome=Outcome.FAILURE,
                        deadline=CLK.observe(t + 2.0),
                    ),
                    Attempt(
                        region="region-b",
                        observer="obs-b",
                        start=loose.observe(t + 10.0),
                        finish=loose.observe(t + 12.2),
                        outcome=Outcome.SUCCESS,
                        deadline=loose.observe(t + 12.0),
                    ),
                ),
            )

        # 预算 2：1 条确定违约 + 1 条可能违约
        definite_req = make_request("d1", 10.0, Outcome.TIMEOUT)
        possible_req = uncertain_request("p1", 20.0)
        reports = [
            ObservationReport(
                observer="obs-a",
                region="region-a",
                covered_intervals=((0.0, 100.0),),
                requests=(definite_req, possible_req),
            )
        ]
        agg = merge_reports("svc", reports)
        by_id = {r.request_id: r for r in agg.requests}
        spec = WindowSpec("w", 100.0, 2)
        outcome = evaluate_window(spec, 100.0, agg.verdicts, by_id)

        self.assertEqual(outcome.definite_violations, 1)
        self.assertEqual(outcome.possible_violations, 1)
        self.assertEqual(outcome.remaining_best, 1)    # 可能违约被证明守约
        self.assertEqual(outcome.remaining_worst, 0)   # 可能违约被证明违约
        self.assertEqual(outcome.status, WindowStatus.AT_RISK)

    def test_possible_only_keeps_window_at_risk_not_exhausted(self) -> None:
        loose = ClockSource("loose", epsilon=0.5)
        # 首跳失败豁免，重试跳落入误差带 -> 仅 1 条可能违约
        req = LogicalRequest(
            request_id="p",
            service="svc",
            origin_region="region-a",
            handed_off=True,
            attempts=(
                Attempt(
                    region="region-a",
                    observer="obs-a",
                    start=loose.observe(0.0),
                    finish=loose.observe(0.5),
                    outcome=Outcome.FAILURE,
                    deadline=loose.observe(2.0),
                ),
                Attempt(
                    region="region-b",
                    observer="obs-b",
                    start=loose.observe(10.0),
                    finish=loose.observe(12.2),
                    outcome=Outcome.SUCCESS,
                    deadline=loose.observe(12.0),
                ),
            ),
        )
        reports = [
            ObservationReport(
                observer="obs-a", region="region-a",
                covered_intervals=((0.0, 100.0),), requests=(req,),
            )
        ]
        agg = merge_reports("svc", reports)
        by_id = {"p": req}
        spec = WindowSpec("w", 100.0, 1)  # 预算 1，确定消耗 0
        outcome = evaluate_window(spec, 100.0, agg.verdicts, by_id)
        self.assertEqual(outcome.possible_violations, 1)
        self.assertEqual(outcome.status, WindowStatus.AT_RISK)
        self.assertEqual((outcome.remaining_best, outcome.remaining_worst), (1, 0))


class SweepTest(unittest.TestCase):
    def test_sweep_and_summary(self) -> None:
        requests = tuple(
            make_request(f"q{i}", t, Outcome.TIMEOUT)
            for i, t in enumerate((10.0, 20.0))
        )
        spec = WindowSpec("w", 100.0, 1)
        times = (100.0, 200.0)
        series = sweep_windows((spec,), tuple(), tuple(), times)
        # 空聚合也应产出每个评估点的结果
        self.assertEqual(len(series["w"]), 2)

        reports = [
            ObservationReport(
                observer="obs-a", region="region-a",
                covered_intervals=((0.0, 300.0),), requests=requests,
            )
        ]
        agg = merge_reports("svc", reports)
        series = sweep_windows(
            (spec,), agg.verdicts, agg.requests, times
        )
        summary = summarize(series["w"], spec)
        self.assertTrue(summary.ever_exhausted)
        self.assertEqual(summary.max_definite, 2)
        self.assertEqual(summary.first_exhausted_at, 100.0)
        # t=200 时窗口 [100,200]，事件全部滑出
        self.assertEqual(series["w"][1].definite_violations, 0)


if __name__ == "__main__":
    unittest.main()
