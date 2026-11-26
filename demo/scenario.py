"""本地合成的一天跨区域切流场景，供演示与测试共用。

场景设定（全部时间为参考时钟秒，horizon = 一天 86400 秒）：

- 两个区域两个时钟源，误差可校对：region-a 的时钟 epsilon=0.2s，
  region-b 的时钟 epsilon=0.5s（备用区结算口径更粗）。
- 服务 checkout 的合同：
    短窗 300s 预算 2；长窗 3600s 预算 7。
- 白天为平稳基线（全部一次成功，不耗预算）；
- 40000s 附近发生一次跨区域切流，切流窗口内产生 10 条带重试/交接
  的逻辑请求，覆盖：
    * 最终成功 + 首跳失败被豁免；
    * 超截止重试的延迟违约在最终成功下仍保留；
    * 最终失败的请求首跳不豁免；
    * 时钟误差导致的"可能违约"；
    * 跨区交接请求被两个区域观察者同时上报（去重场景）。
"""

from __future__ import annotations

from sla_accounting.aggregation import ObservationReport
from sla_accounting.clocks import ClockSource
from sla_accounting.model import (
    Attempt,
    Contract,
    LogicalRequest,
    Outcome,
    WindowSpec,
)

DAY_START = 0.0
DAY_END = 86400.0
HORIZON = (DAY_START, DAY_END)

# 可校对时钟源
CLK_A = ClockSource("clock-region-a", epsilon=0.2)
CLK_B = ClockSource("clock-region-b", epsilon=0.5)

SERVICE = "checkout"
DEADLINE_SECONDS = 2.0  # 每跳相对发出时刻的截止时间


def _deadline(start_reading: float, clock: ClockSource) -> "object":
    return clock.observe(start_reading + DEADLINE_SECONDS)


def _attempt(
    region: str,
    observer: str,
    clock: ClockSource,
    start_reading: float,
    finish_reading: float,
    outcome: Outcome,
) -> Attempt:
    start = clock.observe(start_reading)
    return Attempt(
        region=region,
        observer=observer,
        start=start,
        finish=clock.observe(finish_reading),
        outcome=outcome,
        deadline=_deadline(start_reading, clock),
    )


def build_contract() -> Contract:
    return Contract(
        service=SERVICE,
        windows=(
            WindowSpec("short", length_seconds=300.0, error_budget=2),
            WindowSpec("long", length_seconds=3600.0, error_budget=7),
        ),
    )


def _baseline_request(request_id: str, t: float) -> LogicalRequest:
    """平稳基线：起点区一跳成功，不耗预算。"""
    return LogicalRequest(
        request_id=request_id,
        service=SERVICE,
        origin_region="region-a",
        attempts=(
            _attempt("region-a", "obs-a", CLK_A, t, t + 0.4, Outcome.SUCCESS),
        ),
    )


def build_scenario() -> tuple[Contract, list[ObservationReport]]:
    """返回合同与两个区域观察者的上报。"""
    requests_a: list[LogicalRequest] = []
    requests_b: list[LogicalRequest] = []

    # ---- 平稳基线：每 2000s 一条成功请求，共 43 条 -------------------
    for i in range(43):
        t = 1000.0 + i * 2000.0
        if t >= 86000.0:
            break
        requests_a.append(_baseline_request(f"base-{i:03d}", t))

    # ---- 切流窗口（40000s 附近）的 10 条特殊请求 --------------------
    # r1: 首跳可用性失败、备用区重试成功 -> 首跳豁免，不耗预算
    r1 = LogicalRequest(
        request_id="r1",
        service=SERVICE,
        origin_region="region-a",
        handed_off=True,
        attempts=(
            _attempt("region-a", "obs-a", CLK_A, 40000.0, 40000.8, Outcome.FAILURE),
            _attempt("region-b", "obs-b", CLK_B, 40001.0, 40001.6, Outcome.SUCCESS),
        ),
    )

    # r2: 首跳超时后备用区成功 -> 首跳豁免
    r2 = LogicalRequest(
        request_id="r2",
        service=SERVICE,
        origin_region="region-a",
        handed_off=True,
        attempts=(
            _attempt("region-a", "obs-a", CLK_A, 40010.0, 40013.0, Outcome.TIMEOUT),
            _attempt("region-b", "obs-b", CLK_B, 40014.0, 40015.0, Outcome.SUCCESS),
        ),
    )

    # r3: 最终成功，但第二跳（重试）明确超截止 -> 确定延迟违约，不可消除
    # 超时 1.4s，严格大于 b 时钟总误差带 1.0s -> 确定违约
    r3 = LogicalRequest(
        request_id="r3",
        service=SERVICE,
        origin_region="region-a",
        handed_off=True,
        attempts=(
            _attempt("region-a", "obs-a", CLK_A, 40020.0, 40020.5, Outcome.FAILURE),
            _attempt("region-b", "obs-b", CLK_B, 40021.0, 40024.4, Outcome.SUCCESS),
        ),
    )

    # r4: 最终失败：两跳均失败 -> 首跳不豁免，确定可用性违约
    r4 = LogicalRequest(
        request_id="r4",
        service=SERVICE,
        origin_region="region-a",
        handed_off=True,
        attempts=(
            _attempt("region-a", "obs-a", CLK_A, 40030.0, 40030.9, Outcome.FAILURE),
            _attempt("region-b", "obs-b", CLK_B, 40031.0, 40031.8, Outcome.FAILURE),
        ),
    )

    # r5: 最终成功，但第二跳结算时刻落在截止误差带内 -> 可能违约
    # 起点 40041.0（b 时钟），截止读数 40043.0；结算 40043.2，
    # epsilon=0.5 -> 结算区间 [40042.7, 40043.7]，
    # 截止区间 [40042.5, 40043.5]，两区间相交 -> UNCERTAIN
    r5 = LogicalRequest(
        request_id="r5",
        service=SERVICE,
        origin_region="region-a",
        handed_off=True,
        attempts=(
            _attempt("region-a", "obs-a", CLK_A, 40040.0, 40040.6, Outcome.FAILURE),
            _attempt("region-b", "obs-b", CLK_B, 40041.0, 40043.2, Outcome.SUCCESS),
        ),
    )

    # r6: 纯起点区请求，跳超时且最终失败 -> 确定违约
    r6 = LogicalRequest(
        request_id="r6",
        service=SERVICE,
        origin_region="region-a",
        attempts=(
            _attempt("region-a", "obs-a", CLK_A, 40050.0, 40053.5, Outcome.TIMEOUT),
        ),
    )

    # r7: 最终成功，第二跳明确超时 -> 确定违约
    r7 = LogicalRequest(
        request_id="r7",
        service=SERVICE,
        origin_region="region-a",
        handed_off=True,
        attempts=(
            _attempt("region-a", "obs-a", CLK_A, 40060.0, 40060.7, Outcome.FAILURE),
            _attempt("region-b", "obs-b", CLK_B, 40061.0, 40064.5, Outcome.SUCCESS),
        ),
    )

    # r8: 最终成功，第二跳在误差带内 -> 可能违约
    # b 时钟起点 40071.0，截止读数 40073.0；结算 40073.35
    r8 = LogicalRequest(
        request_id="r8",
        service=SERVICE,
        origin_region="region-a",
        handed_off=True,
        attempts=(
            _attempt("region-a", "obs-a", CLK_A, 40070.0, 40070.5, Outcome.FAILURE),
            _attempt("region-b", "obs-b", CLK_B, 40071.0, 40073.35, Outcome.SUCCESS),
        ),
    )

    # r9: 最终失败，第二跳延迟违约 + 可用性失败 -> 确定违约（单事件）
    r9 = LogicalRequest(
        request_id="r9",
        service=SERVICE,
        origin_region="region-a",
        handed_off=True,
        attempts=(
            _attempt("region-a", "obs-a", CLK_A, 40080.0, 40080.6, Outcome.FAILURE),
            _attempt("region-b", "obs-b", CLK_B, 40081.0, 40084.2, Outcome.FAILURE),
        ),
    )

    # r10: 最终成功，两跳均守约 -> 不违约
    r10 = LogicalRequest(
        request_id="r10",
        service=SERVICE,
        origin_region="region-a",
        handed_off=True,
        attempts=(
            _attempt("region-a", "obs-a", CLK_A, 40090.0, 40090.5, Outcome.FAILURE),
            _attempt("region-b", "obs-b", CLK_B, 40091.0, 40092.0, Outcome.SUCCESS),
        ),
    )

    burst = [r1, r2, r3, r4, r5, r6, r7, r8, r9, r10]

    # 跨区交接请求在两个区域观察者处各出现一次（同 id，同内容 -> 重传去重）。
    # obs-a 看到全链路（它自己的跳 + 备用区回传的跳），obs-b 同样看到全链路。
    for request in burst:
        requests_a.append(request)
        if request.handed_off:
            requests_b.append(request)

    report_a = ObservationReport(
        observer="obs-a",
        region="region-a",
        covered_intervals=((0.0, 50000.0), (60000.0, 86400.0)),
        requests=tuple(requests_a),
    )
    report_b = ObservationReport(
        observer="obs-b",
        region="region-b",
        # 备用区观察者覆盖切流时段，并与 obs-a 的覆盖区间相交/相接
        covered_intervals=((39000.0, 61000.0),),
        requests=tuple(requests_b),
    )
    return build_contract(), [report_a, report_b]
