"""影响归属模块：把入口消息批次中的处理结果归属到原始前端请求。

背景：入口的多条前端请求被汇成一个消息批次后，链路上的后端错误经常被
无差别地画到批次内所有请求上。本模块根据以下输入为每个原始请求单独判定
最终状态，避免"一个成员出错、整批背锅"：

* 前端请求生成的消息编号（request_id <-> message_id）；
* 批次的成员名单与拆批（split/retry）父子关系；
* 每个消息各自必需的处理步骤（有序）；
* 带尝试号的步骤处理结果：一个步骤可以只报告部分消息失败，也可以只
  确认部分成员，其余成员依自己的步骤结果判定。

归属规则概要：

* 每个消息沿自己的"批次链"（初始批次 -> 拆批子批次 -> ...）按必需步骤
  顺序推进；步骤一旦通过即保持通过，失败后在同批次更高尝试或拆批子批次
  中重新确认。
* 同一批次同一步骤的多次尝试，以最大尝试号为权威结果；路径中保留全部
  尝试记录，失败与成功都归属到明确的 批次 + 步骤 + 尝试号。
* 最终状态：
    - SUCCESS：全部必需步骤在某个明确尝试中通过；
    - FAILED：权威结果失败，且没有后续拆批重试接管；
    - PENDING（待查）：缺少该成员的最终确认（无报告、报告未覆盖该成员，
      或拆批重试已发生但尚无结果）。
* 查询按原始 request_id 返回状态及跨批次、跨尝试的关联路径。

仅使用标准库，适配 Python 3.11。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple

__all__ = [
    "Status",
    "Outcome",
    "StepOutcome",
    "FailureInfo",
    "Attribution",
    "AttributionEngine",
]


class Status(str, Enum):
    """请求级最终状态。"""

    SUCCESS = "success"
    FAILED = "failed"
    PENDING = "pending"  # 待查：缺少该成员的最终确认


class Outcome(str, Enum):
    """关联路径中单个步骤单次尝试的结果。"""

    PASSED = "passed"
    FAILED = "failed"
    UNCONFIRMED = "unconfirmed"  # 报告存在但未覆盖该成员


@dataclass(frozen=True)
class StepOutcome:
    """路径条目：某批次、某步骤、某次尝试对该消息的结果。"""

    batch_id: str
    step: str
    attempt: int
    outcome: Outcome


@dataclass(frozen=True)
class FailureInfo:
    """最终失败的归属：明确的批次 + 步骤 + 尝试号。"""

    batch_id: str
    step: str
    attempt: int


@dataclass(frozen=True)
class Attribution:
    """单个原始请求的归属结果。"""

    request_id: str
    message_id: str
    status: Status
    path: Tuple[StepOutcome, ...]  # 跨批次、跨尝试的关联路径
    failure: Optional[FailureInfo]  # 仅 FAILED 时非空


@dataclass(frozen=True)
class _Message:
    message_id: str
    request_id: str
    steps: Tuple[str, ...]


@dataclass(frozen=True)
class _Batch:
    batch_id: str
    message_ids: frozenset
    parent_batch_id: Optional[str]  # 拆批关系：指向来源批次


@dataclass(frozen=True)
class _Report:
    batch_id: str
    step: str
    attempt: int
    failed: frozenset  # 本次尝试中失败的消息子集
    confirmed: Optional[frozenset]  # None 表示覆盖批次全体成员

    def covers(self, message_id: str, batch: _Batch) -> bool:
        if self.confirmed is None:
            return message_id in batch.message_ids
        return message_id in self.confirmed


def _require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} 必须是非空字符串")


class AttributionEngine:
    """影响归属引擎。

    使用顺序：register_message -> register_batch（拆批时指定
    parent_batch_id）-> record_result -> query_request / attribute_all。
    录入顺序中，父批次必须先于子批次注册。
    """

    def __init__(self) -> None:
        self._messages: Dict[str, _Message] = {}
        self._batches: Dict[str, _Batch] = {}
        self._reports: Dict[Tuple[str, str, int], _Report] = {}
        self._request_index: Dict[str, str] = {}  # request_id -> message_id

    # ------------------------------------------------------------------
    # 数据录入
    # ------------------------------------------------------------------
    def register_message(
        self, message_id: str, request_id: str, steps: Iterable[str]
    ) -> None:
        """登记前端请求生成的消息及其必需处理步骤（有序）。"""
        _require_text(message_id, "message_id")
        _require_text(request_id, "request_id")
        steps = tuple(steps)
        if not steps:
            raise ValueError("steps 不能为空：每个消息至少有一个必需步骤")
        for step in steps:
            _require_text(step, "step")
        if len(set(steps)) != len(steps):
            raise ValueError(f"消息 {message_id} 的步骤列表存在重复")
        if message_id in self._messages:
            raise ValueError(f"消息 {message_id} 已登记")
        if request_id in self._request_index:
            raise ValueError(f"请求 {request_id} 已登记")
        self._messages[message_id] = _Message(message_id, request_id, steps)
        self._request_index[request_id] = message_id

    def register_batch(
        self,
        batch_id: str,
        message_ids: Iterable[str],
        parent_batch_id: Optional[str] = None,
    ) -> None:
        """登记批次成员；拆批重试时用 parent_batch_id 指向来源批次。"""
        _require_text(batch_id, "batch_id")
        if batch_id in self._batches:
            raise ValueError(f"批次 {batch_id} 已登记")
        members = frozenset(message_ids)
        if not members:
            raise ValueError("批次成员不能为空")
        unknown = members - self._messages.keys()
        if unknown:
            raise ValueError(f"批次 {batch_id} 含未登记消息: {sorted(unknown)}")
        if parent_batch_id is not None:
            if parent_batch_id == batch_id:
                raise ValueError("批次不能以自身为父批次")
            if parent_batch_id not in self._batches:
                raise ValueError(f"父批次 {parent_batch_id} 未登记")
        self._batches[batch_id] = _Batch(batch_id, members, parent_batch_id)

    def record_result(
        self,
        batch_id: str,
        step: str,
        attempt: int,
        failed_message_ids: Iterable[str] = (),
        confirmed_message_ids: Optional[Iterable[str]] = None,
    ) -> None:
        """录入某批次某步骤某次尝试的处理结果。

        failed_message_ids: 本次尝试中失败的消息子集；被覆盖但未列入其中的
            成员视为该步骤本次尝试通过。
        confirmed_message_ids: 本次结果覆盖的成员集合；为 None 时表示覆盖
            批次全体成员。批次内未被覆盖的成员属于"缺少确认"。
        """
        _require_text(step, "step")
        if batch_id not in self._batches:
            raise ValueError(f"批次 {batch_id} 未登记")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ValueError("attempt 必须是 >= 1 的整数")
        batch = self._batches[batch_id]
        failed = frozenset(failed_message_ids)
        if not failed <= batch.message_ids:
            raise ValueError(
                f"failed_message_ids 含批次外消息: {sorted(failed - batch.message_ids)}"
            )
        confirmed: Optional[frozenset]
        if confirmed_message_ids is None:
            confirmed = None
        else:
            confirmed = frozenset(confirmed_message_ids)
            if not confirmed:
                raise ValueError("confirmed_message_ids 不能为空集合")
            if not confirmed <= batch.message_ids:
                raise ValueError(
                    "confirmed_message_ids 含批次外消息: "
                    f"{sorted(confirmed - batch.message_ids)}"
                )
            if not failed <= confirmed:
                raise ValueError("failed_message_ids 必须是被确认成员的子集")
        key = (batch_id, step, attempt)
        if key in self._reports:
            raise ValueError(
                f"批次 {batch_id} 步骤 {step} 尝试 {attempt} 的结果已录入"
            )
        covered = confirmed if confirmed is not None else batch.message_ids
        if not any(step in self._messages[mid].steps for mid in covered):
            raise ValueError(
                f"步骤 {step} 不是本次覆盖成员中任何消息的必需步骤"
            )
        self._reports[key] = _Report(batch_id, step, attempt, failed, confirmed)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def query_request(self, request_id: str) -> Attribution:
        """按原始请求编号查询归属结果（状态 + 关联路径）。"""
        if request_id not in self._request_index:
            raise KeyError(f"未知请求: {request_id}")
        return self._attribute(self._messages[self._request_index[request_id]])

    def attribute_all(self) -> Dict[str, Attribution]:
        """对所有已登记请求做归属，返回 request_id -> Attribution。"""
        return {
            msg.request_id: self._attribute(msg) for msg in self._messages.values()
        }

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _chain_for(self, message_id: str) -> List[_Batch]:
        """构造消息走过的批次链：初始批次 -> 拆批子批次 -> ..."""
        containing = [b for b in self._batches.values() if message_id in b.message_ids]
        if not containing:
            return []
        containing_ids = {b.batch_id for b in containing}
        roots = [
            b
            for b in containing
            if b.parent_batch_id is None or b.parent_batch_id not in containing_ids
        ]
        if len(roots) != 1:
            raise ValueError(
                f"消息 {message_id} 出现在多个互不衔接的批次中: "
                f"{sorted(b.batch_id for b in roots)}"
            )
        chain: List[_Batch] = []
        current: Optional[_Batch] = roots[0]
        visited = set()
        while current is not None:
            if current.batch_id in visited:
                raise ValueError(f"消息 {message_id} 的批次链存在环")
            visited.add(current.batch_id)
            chain.append(current)
            children = [
                b for b in containing if b.parent_batch_id == current.batch_id
            ]
            if len(children) > 1:
                raise ValueError(
                    f"消息 {message_id} 在批次 {current.batch_id} 后被拆入多个"
                    "子批次，无法确定唯一重试路径"
                )
            current = children[0] if children else None
        return chain

    def _reports_for(self, batch_id: str, step: str) -> List[_Report]:
        """某批次某步骤的全部尝试报告，按尝试号升序。"""
        return sorted(
            (
                report
                for (bid, s, _), report in self._reports.items()
                if bid == batch_id and s == step
            ),
            key=lambda r: r.attempt,
        )

    def _attribute(self, msg: _Message) -> Attribution:
        chain = self._chain_for(msg.message_id)
        if not chain:
            # 消息未进入任何批次：没有任何确认，待查。
            return Attribution(msg.request_id, msg.message_id, Status.PENDING, (), None)

        path: List[StepOutcome] = []
        progress = 0  # 下一个待确认步骤的下标；已通过的步骤保持通过
        last_failure: Optional[FailureInfo] = None
        last_event_batch_index = -1

        for batch_index, batch in enumerate(chain):
            while progress < len(msg.steps):
                step = msg.steps[progress]
                reports = self._reports_for(batch.batch_id, step)
                if not reports:
                    break  # 该批次此步骤无任何报告，交给后续批次
                for report in reports:
                    if not report.covers(msg.message_id, batch):
                        outcome = Outcome.UNCONFIRMED
                    elif msg.message_id in report.failed:
                        outcome = Outcome.FAILED
                    else:
                        outcome = Outcome.PASSED
                    path.append(StepOutcome(batch.batch_id, step, report.attempt, outcome))
                last_event_batch_index = batch_index
                authoritative = reports[-1]  # 最大尝试号为权威结果
                if not authoritative.covers(msg.message_id, batch):
                    break  # 缺少该成员的最终确认
                if msg.message_id in authoritative.failed:
                    last_failure = FailureInfo(
                        batch.batch_id, step, authoritative.attempt
                    )
                    break  # 失败，等待同批次更高尝试或拆批子批次重新确认
                progress += 1

        if progress >= len(msg.steps):
            return Attribution(
                msg.request_id, msg.message_id, Status.SUCCESS, tuple(path), None
            )
        last_entry = path[-1] if path else None
        if (
            last_entry is not None
            and last_entry.outcome is Outcome.FAILED
            and last_event_batch_index == len(chain) - 1
        ):
            # 权威结果为失败，且没有后续拆批重试接管：失败归到明确尝试。
            return Attribution(
                msg.request_id, msg.message_id, Status.FAILED, tuple(path), last_failure
            )
        # 其余情况均缺少该成员的最终确认：待查。
        return Attribution(
            msg.request_id, msg.message_id, Status.PENDING, tuple(path), None
        )
