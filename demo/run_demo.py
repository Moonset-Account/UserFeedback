"""命令行演示：跑一遍合成的一天切流场景并打印核算结果。

运行方式（在工作区根目录）：

    python3 -m demo.run_demo
"""

from __future__ import annotations

from sla_accounting import (
    OfflineBudgetCalculator,
    build_day_sweep,
)
from sla_accounting.accounting import ViolationStatus as Status
from sla_accounting.windows import WindowStatus

from .scenario import HORIZON, build_scenario

STATUS_CN = {
    Status.DEFINITE: "确定违约",
    Status.POSSIBLE: "可能违约",
    Status.NONE: "未违约",
}
WINDOW_STATUS_CN = {
    WindowStatus.OK: "正常",
    WindowStatus.AT_RISK: "风险",
    WindowStatus.EXHAUSTED: "耗尽",
}


def main() -> None:
    contract, reports = build_scenario()
    eval_times = build_day_sweep(HORIZON, tick_seconds=60.0)
    calculator = OfflineBudgetCalculator(contract, horizon=HORIZON)
    result = calculator.evaluate(reports, eval_times)

    agg = result.aggregate
    print("=" * 72)
    print(f"服务: {contract.service}  观察者: {sorted(agg.observers)}")
    print(f"去重后逻辑请求数: {len(agg.requests)}（忽略重传 {agg.duplicate_reports_ignored} 条）")
    print(f"观测覆盖区间: {agg.coverage}")
    print(f"观测缺口: {agg.gaps if agg.gaps else '无（全天覆盖）'}")
    print("-" * 72)
    print("切流窗口内各请求核算明细：")
    for verdict in agg.verdicts:
        if not verdict.request_id.startswith("r"):
            continue
        tag = "交接" if verdict.handed_off else "本区"
        tail = "最终成功" if verdict.succeeded else "最终失败"
        print(
            f"  {verdict.request_id:>4} [{tag}|{tail}] "
            f"{STATUS_CN[verdict.status]:<6} "
            f"确定违约明细={verdict.definite_violations} "
            f"可能={verdict.possible_violations} "
            f"豁免可用性={verdict.excused_availability} "
            f"豁免延迟={verdict.excused_latency}"
        )

    print("-" * 72)
    for spec in contract.windows:
        summary = result.window_summaries[spec.name]
        print(
            f"窗口 {spec.name}（{spec.length_seconds:.0f}s，预算 {spec.error_budget}）: "
            f"峰值确定违约={summary.max_definite}，峰值可能违约={summary.max_possible}，"
            f"剩余额度最好={summary.min_remaining_best} / 最坏={summary.min_remaining_worst}，"
            f"是否曾耗尽={summary.ever_exhausted}，是否曾风险={summary.ever_at_risk}"
        )

    # 切流峰值时刻（40200s）的短窗/长窗快照
    peak_at = 40200.0
    print("-" * 72)
    print(f"切流峰值 t={peak_at:.0f}s 窗口快照：")
    for spec in contract.windows:
        outcome = result.outcome_at(spec.name, peak_at)
        print(
            f"  {spec.name:>5}: {WINDOW_STATUS_CN[outcome.status]:<4} "
            f"确定={outcome.definite_violations} 可能={outcome.possible_violations} "
            f"剩余最好={outcome.remaining_best} 剩余最坏={outcome.remaining_worst}"
        )
    print("=" * 72)


if __name__ == "__main__":
    main()
