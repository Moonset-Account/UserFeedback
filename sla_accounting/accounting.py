"""单条逻辑请求的违约判定。

核算规则（对应题面）：

1. 一次**最终成功**的逻辑请求可以免记**首跳**失败：
   - 首跳的可用性失败（FAILURE/TIMEOUT）豁免；
   - 首跳即使超过截止时间，作为首跳也一并豁免。
2. 超出截止时间的重试**不能消除已经发生的违约**：
   - 首跳之后的任何一跳，只要结算时刻晚于该跳截止时间，就记一次
     延迟违约；无论最终是否成功，这条违约都保留；
   - 首跳之后的可用性失败不享受豁免，记一次可用性违约。
3. 时钟误差使截止时间判定呈现三态：
   - 确定超时 -> 确定违约；
   - 无法确定 -> 可能违约；
   - 确定赶上 -> 不违约。
4. 一条逻辑请求在一个预算窗口内最多消耗 1 个违约事件额度：
   - 含任一确定违约 -> 确定违约事件；
   - 否则含任一可能违约 -> 可能违约事件；
   - 否则不消耗预算。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .clocks import Ternary, meets_deadline
from .model import AVAILABILITY_FAILURES, Attempt, LogicalRequest, Outcome


class ViolationStatus(str):
    """违约事件在预算口径下的三态标签。"""

    DEFINITE = "definite"    # 确定违约
    POSSIBLE = "possible"    # 可能违约（时钟不确定性）
    NONE = "none"            # 不违约


@dataclass(frozen=True)
class AttemptMark:
    """单跳的判定明细。"""

    index: int
    region: str
    outcome: Outcome
    deadline_verdict: Ternary       # 结算时刻 vs 截止时间
    availability_violation: bool    # 是否计入可用性违约
    latency_violation: ViolationStatus  # 延迟违约三态
    excused: bool                   # 是否适用首跳豁免


@dataclass(frozen=True)
class RequestVerdict:
    """一条逻辑请求的完整核算结论。"""

    request_id: str
    service: str
    origin_region: str
    handed_off: bool
    succeeded: bool
    marks: tuple[AttemptMark, ...]
    status: ViolationStatus                 # 预算事件三态
    definite_violations: int                # 确定违约条数（明细口径）
    possible_violations: int                # 可能违约条数（明细口径）
    excused_availability: int               # 被豁免的可用性失败数
    excused_latency: int                    # 被豁免的延迟违约数

    @property
    def consumes_budget(self) -> bool:
        return self.status is not ViolationStatus.NONE


def _classify_latency(attempt: Attempt) -> ViolationStatus:
    """根据结算时刻与截止时间的三态比较给出延迟违约标签。"""
    verdict = meets_deadline(attempt.finish, attempt.deadline)
    if verdict is Ternary.FALSE:
        return ViolationStatus.DEFINITE
    if verdict is Ternary.UNCERTAIN:
        return ViolationStatus.POSSIBLE
    return ViolationStatus.NONE


def evaluate_request(request: LogicalRequest) -> RequestVerdict:
    """对一条逻辑请求执行离线核算。"""
    marks: list[AttemptMark] = []
    definite = 0
    possible = 0
    excused_avail = 0
    excused_latency = 0

    # 首跳豁免仅在逻辑请求最终成功时适用。
    first_hop_excused = request.succeeded

    for index, attempt in enumerate(request.attempts):
        is_first_hop = index == 0
        latency = _classify_latency(attempt)
        availability_failed = attempt.outcome in AVAILABILITY_FAILURES

        excused = False
        availability_violation = False

        if is_first_hop and first_hop_excused:
            # 最终成功：首跳的可用性失败与首跳延迟违约免记。
            excused = True
            if availability_failed:
                excused_avail += 1
            if latency is ViolationStatus.DEFINITE:
                excused_latency += 1
            elif latency is ViolationStatus.POSSIBLE:
                # 可能违约被豁免后即不进入预算（成功结果已确定该跳被重试覆盖）。
                excused_latency += 1
            latency = ViolationStatus.NONE
        else:
            if availability_failed:
                availability_violation = True
                definite += 1

        if latency is ViolationStatus.DEFINITE:
            definite += 1
        elif latency is ViolationStatus.POSSIBLE:
            possible += 1

        marks.append(
            AttemptMark(
                index=index,
                region=attempt.region,
                outcome=attempt.outcome,
                deadline_verdict=meets_deadline(attempt.finish, attempt.deadline),
                availability_violation=availability_violation,
                latency_violation=latency,
                excused=excused,
            )
        )

    if definite > 0:
        status = ViolationStatus.DEFINITE
    elif possible > 0:
        status = ViolationStatus.POSSIBLE
    else:
        status = ViolationStatus.NONE

    return RequestVerdict(
        request_id=request.request_id,
        service=request.service,
        origin_region=request.origin_region,
        handed_off=request.handed_off,
        succeeded=request.succeeded,
        marks=tuple(marks),
        status=status,
        definite_violations=definite,
        possible_violations=possible,
        excused_availability=excused_avail,
        excused_latency=excused_latency,
    )
