"""端到端测试：合成的一天切流场景，短窗与长窗触发不同处理。"""

import unittest

from sla_accounting import (
    OfflineBudgetCalculator,
    build_day_sweep,
)
from sla_accounting.accounting import ViolationStatus
from sla_accounting.windows import WindowStatus

from demo.scenario import (
    DAY_END,
    HORIZON,
    build_contract,
    build_scenario,
)


class SyntheticDayScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract, cls.reports = build_scenario()
        cls.eval_times = build_day_sweep(HORIZON, tick_seconds=60.0)
        cls.result = OfflineBudgetCalculator(
            cls.contract, horizon=HORIZON
        ).evaluate(cls.reports, cls.eval_times)
        cls.agg = cls.result.aggregate

    def test_handoff_requests_deduplicated_across_regions(self) -> None:
        # 43 条基线 + 10 条切流请求；9 条交接请求被 obs-b 重复上报后去重
        self.assertEqual(len(self.agg.requests), 53)
        self.assertEqual(self.agg.duplicate_reports_ignored, 9)
        ids = [r.request_id for r in self.agg.requests]
        self.assertEqual(len(ids), len(set(ids)))  # 服务级无重复 id

    def test_coverage_merged_without_gap(self) -> None:
        self.assertEqual(self.agg.coverage, ((0.0, DAY_END),))
        self.assertEqual(self.agg.gaps, ())

    # ---- 单请求裁决：首跳豁免与超期重试不可消除 --------------------
    def test_first_hop_failures_excused_when_final_success(self) -> None:
        for rid in ("r1", "r2", "r10"):
            verdict = self.agg.verdict_of(rid)
            self.assertEqual(
                verdict.status, ViolationStatus.NONE,
                msg=f"{rid} 最终成功且仅首跳失败，应豁免",
            )
            self.assertGreaterEqual(verdict.excused_availability, 1)

    def test_late_retry_violation_survives_final_success(self) -> None:
        r3 = self.agg.verdict_of("r3")
        self.assertEqual(r3.status, ViolationStatus.DEFINITE)
        self.assertEqual(r3.excused_availability, 1)  # 首跳仍豁免
        self.assertEqual(r3.definite_violations, 1)   # 重试延迟违约保留
        self.assertTrue(r3.succeeded)

    def test_final_failure_no_excuse(self) -> None:
        for rid in ("r4", "r6", "r9"):
            verdict = self.agg.verdict_of(rid)
            self.assertEqual(verdict.status, ViolationStatus.DEFINITE)
            self.assertEqual(verdict.excused_availability, 0)

    def test_clock_uncertainty_yields_possible(self) -> None:
        for rid in ("r5", "r8"):
            verdict = self.agg.verdict_of(rid)
            self.assertEqual(verdict.status, ViolationStatus.POSSIBLE)
            self.assertEqual(verdict.possible_violations, 1)
            self.assertEqual(verdict.definite_violations, 0)

    def test_r7_definite_latency_after_excused_first_hop(self) -> None:
        r7 = self.agg.verdict_of("r7")
        self.assertEqual(r7.status, ViolationStatus.DEFINITE)
        self.assertEqual(r7.excused_availability, 1)

    # ---- 切流峰值：短窗耗尽 vs 长窗风险 -----------------------------
    def test_short_window_exhausted_but_long_window_only_at_risk(self) -> None:
        peak = self.result.outcome_at("short", 40200.0)
        long_peak = self.result.outcome_at("long", 40200.0)

        self.assertEqual(peak.status, WindowStatus.EXHAUSTED)
        self.assertEqual(peak.definite_violations, 5)  # r3,r4,r6,r7,r9
        self.assertEqual(peak.possible_violations, 2)  # r5,r8

        self.assertEqual(long_peak.status, WindowStatus.AT_RISK)
        self.assertEqual(long_peak.definite_violations, peak.definite_violations)
        # 长窗预算 7：最好剩 2，最坏恰为 0，尚未被确定违约耗尽
        self.assertEqual(long_peak.remaining_best, 2)
        self.assertEqual(long_peak.remaining_worst, 0)

    def test_peak_counts_match_request_level_verdicts(self) -> None:
        # 交叉校验：窗口计数必须等于窗口内各请求裁决之和
        peak = self.result.outcome_at("short", 40200.0)
        in_window = [
            self.agg.verdict_of(rid) for rid in peak.request_ids
            if rid.startswith("r")
        ]
        definite = sum(
            1 for v in in_window if v.status == ViolationStatus.DEFINITE
        )
        possible = sum(
            1 for v in in_window if v.status == ViolationStatus.POSSIBLE
        )
        self.assertEqual(definite, peak.definite_violations)
        self.assertEqual(possible, peak.possible_violations)

    def test_budget_recovers_after_burst_slides_out(self) -> None:
        # 切流后足够久（长窗之后），两个窗口都应回到正常
        tail = self.result.outcome_at("short", 86400.0)
        tail_long = self.result.outcome_at("long", 86400.0)
        self.assertEqual(tail.status, WindowStatus.OK)
        self.assertEqual(tail_long.status, WindowStatus.OK)
        self.assertEqual(tail.definite_violations, 0)

    def test_day_summaries(self) -> None:
        short = self.result.window_summaries["short"]
        long = self.result.window_summaries["long"]
        self.assertTrue(short.ever_exhausted)
        self.assertFalse(long.ever_exhausted)
        self.assertTrue(long.ever_at_risk)
        self.assertIsNotNone(short.first_exhausted_at)
        # 短窗最坏剩余为负（超额违约），长窗最坏剩余恰为 0
        self.assertLess(short.min_remaining_worst, 0)
        self.assertEqual(long.min_remaining_worst, 0)

    def test_baseline_requests_never_consume_budget(self) -> None:
        baseline = [
            v for v in self.agg.verdicts if v.request_id.startswith("base-")
        ]
        self.assertEqual(len(baseline), 43)
        self.assertTrue(all(v.status == ViolationStatus.NONE for v in baseline))

    def test_sweep_covers_full_day_with_minute_ticks(self) -> None:
        # 60s 步长：40200 是 60 的整数倍，必须恰好出现在评估点上
        self.assertIn(40200.0, self.eval_times)
        self.assertEqual(self.eval_times[-1], DAY_END)
        for name in ("short", "long"):
            self.assertEqual(
                len(self.result.window_timeseries[name]), len(self.eval_times)
            )


if __name__ == "__main__":
    unittest.main()
