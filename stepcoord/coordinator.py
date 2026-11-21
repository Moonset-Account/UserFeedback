"""同步训练步协调器。

每个训练步中，各 rank 会为若干微批上报：

* 该微批的有效 Token 数；
* 已经按 Token 求过平均的梯度向量；
* 是否发生数值溢出。

协调器依据初始化时给定的微批名单（roster）收齐这些贡献，用全局有效
Token 总数归一，再执行 ``theta -= lr * normalized_gradient``。

关键语义：

* 权重为 ``tokens_i / sum(tokens)``，零 Token 微批权重为 0、梯度被忽略；
* 整步零 Token、任一条上报 overflow、或出现冲突重传时，该步不更新参数；
* 学习率位置（已成功更新次数）只在参数真正更新时前进；跳过的步仍会推进
  步号；
* 同一微批在结算前的相同重传只计一次，冲突重传使该步失败；
* 某步结算后，属于该步的回执一律视为旧回执，不改变任何状态。

本模块是纯本地组件：不涉及网络传输、序列化或优化器状态。
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Sequence


class CoordinatorError(Exception):
    """协调器错误基类。"""


class UnknownMicroBatchError(CoordinatorError):
    """上报的微批不在预先给定的名单中。"""

    def __init__(self, mb_id: str) -> None:
        self.mb_id = mb_id
        super().__init__(f"micro-batch {mb_id!r} is not in the step roster")


class RankMismatchError(CoordinatorError):
    """上报的 rank 与名单中该微批的归属 rank 不一致。"""

    def __init__(self, mb_id: str, expected_rank: int, actual_rank: int) -> None:
        self.mb_id = mb_id
        self.expected_rank = expected_rank
        self.actual_rank = actual_rank
        super().__init__(
            f"micro-batch {mb_id!r} belongs to rank {expected_rank}, "
            f"but the report came from rank {actual_rank}"
        )


class FutureStepError(CoordinatorError):
    """上报的步号超前于协调器当前步。"""

    def __init__(self, current_step: int, report_step: int) -> None:
        self.current_step = current_step
        self.report_step = report_step
        super().__init__(
            f"report targets step {report_step}, but the coordinator is at "
            f"step {current_step}; settle the current step first"
        )


class IncompleteStepError(CoordinatorError):
    """名单中的微批尚未收齐（且该步尚未因冲突被判失败）。"""

    def __init__(self, step: int, missing: Sequence[str]) -> None:
        self.step = step
        self.missing = tuple(missing)
        listed = ", ".join(repr(mb_id) for mb_id in self.missing)
        super().__init__(
            f"step {step} is missing contributions for micro-batches: {listed}"
        )


class LearningRateScheduleError(CoordinatorError):
    """当前学习率位置无法从学习率表取得有效值。"""


class StepStatus(str, Enum):
    """一次结算的结果状态。"""

    APPLIED = "applied"
    SKIPPED_NO_TOKENS = "skipped_no_tokens"
    SKIPPED_OVERFLOW = "skipped_overflow"
    FAILED_CONFLICT = "failed_conflict"


class SubmitResult(str, Enum):
    """单条上报被接收时的结果。"""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    STEP_FAILED = "step_failed"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class MicroBatch:
    """预先给定的微批名单条目。

    ``mb_id`` 在名单中唯一，``rank`` 是该微批的唯一上报方。
    """

    mb_id: str
    rank: int

    def __post_init__(self) -> None:
        if not isinstance(self.mb_id, str) or not self.mb_id:
            raise ValueError("mb_id must be a non-empty string")
        if (
            isinstance(self.rank, bool)
            or not isinstance(self.rank, int)
            or self.rank < 0
        ):
            raise ValueError("rank must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class GradientReport:
    """某个 rank 为某个微批提交的上报。

    ``gradient`` 是该微批有效 Token 上的平均梯度；提交时由协调器校验其长度
    是否等于参数维度、各分量是否有限。零 Token 微批没有数值贡献、overflow
    的微批会被整步跳过，因此这两类上报可以省略梯度，也可以携带任意内容
    （例如 overflow 时产生的 ``inf``/``nan``）：未参与计算的梯度既不校验也
    不参与重传比较。
    """

    step: int
    mb_id: str
    rank: int
    valid_tokens: int
    gradient: Sequence[float] | None = None
    overflow: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.step, bool)
            or not isinstance(self.step, int)
            or self.step < 0
        ):
            raise ValueError("step must be a non-negative integer")
        if not isinstance(self.mb_id, str) or not self.mb_id:
            raise ValueError("mb_id must be a non-empty string")
        if (
            isinstance(self.rank, bool)
            or not isinstance(self.rank, int)
            or self.rank < 0
        ):
            raise ValueError("rank must be a non-negative integer")
        if (
            isinstance(self.valid_tokens, bool)
            or not isinstance(self.valid_tokens, int)
            or self.valid_tokens < 0
        ):
            raise ValueError("valid_tokens must be a non-negative integer")
        if not isinstance(self.overflow, bool):
            raise ValueError("overflow must be a bool")

        contributes = self.valid_tokens > 0 and not self.overflow
        if self.gradient is None:
            if contributes:
                raise ValueError(
                    "a micro-batch with valid tokens and no overflow must "
                    "carry a gradient"
                )
            return
        try:
            values = tuple(float(value) for value in self.gradient)
        except (TypeError, ValueError) as exc:
            raise ValueError("gradient must be a sequence of numbers") from exc
        if contributes and not all(math.isfinite(value) for value in values):
            raise ValueError("gradient components must be finite")
        object.__setattr__(self, "gradient", values)


@dataclass(frozen=True, slots=True)
class Contribution:
    """结算结果中记录的单个微批贡献。

    ``weight`` 是全局 Token 权重，仅成功更新（``APPLIED``）时有值，跳过或
    失败时为 ``None``。
    """

    mb_id: str
    rank: int
    valid_tokens: int
    weight: float | None


@dataclass(frozen=True, slots=True)
class StepResult:
    """一次结算的不可变快照。

    * ``lr_position``：本次结算使用的位置；未更新的步为尚未推进的位置；
    * ``learning_rate``：实际用于更新的学习率，没有更新时为 ``None``；
    * ``parameters``：结算后的参数快照。
    """

    step: int
    status: StepStatus
    total_valid_tokens: int
    lr_position: int
    learning_rate: float | None
    parameters: tuple[float, ...]
    contributions: tuple[Contribution, ...]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _Payload:
    """用于重传比较的规范化载荷。

    零 Token 或 overflow 的梯度不参与计算，因此也不参与身份比较。
    """

    valid_tokens: int
    overflow: bool
    gradient: tuple[float, ...] | None


class StepCoordinator:
    """收集各微批贡献并结算单步参数更新。

    学习率按“已成功更新次数”索引：第 ``position`` 次成功更新读取
    ``lr_schedule[position]``（或调用 ``lr_schedule(position)``），成功后
    位置加一。跳过的步或失败的步不会推进学习率位置。

    参数：

    * ``parameters``：一维参数向量，内部复制；
    * ``roster``：预先给定的微批名单，每项唯一且归属固定 rank；
    * ``lr_schedule``：学习率序列或接收位置的回调。
    """

    def __init__(
        self,
        parameters: Sequence[float],
        roster: Iterable[MicroBatch],
        *,
        lr_schedule: Callable[[int], float] | Sequence[float],
    ) -> None:
        try:
            params = tuple(float(value) for value in parameters)
        except (TypeError, ValueError) as exc:
            raise ValueError("parameters must be a sequence of numbers") from exc
        if not params:
            raise ValueError("parameters must not be empty")
        if not all(math.isfinite(value) for value in params):
            raise ValueError("parameters must be finite")

        roster_items = tuple(roster)
        if not roster_items:
            raise ValueError("roster must not be empty")
        seen: set[str] = set()
        for micro_batch in roster_items:
            if not isinstance(micro_batch, MicroBatch):
                raise TypeError("roster entries must be MicroBatch instances")
            if micro_batch.mb_id in seen:
                raise ValueError(
                    f"duplicate micro-batch id in roster: {micro_batch.mb_id!r}"
                )
            seen.add(micro_batch.mb_id)

        self._dim = len(params)
        self._params = list(params)
        self._roster = roster_items
        self._order = tuple(micro_batch.mb_id for micro_batch in roster_items)
        self._by_id = {micro_batch.mb_id: micro_batch for micro_batch in roster_items}

        self._lr_callable: Callable[[int], float] | None
        self._lr_values: tuple[float, ...] | None
        if callable(lr_schedule):
            self._lr_callable = lr_schedule
            self._lr_values = None
        else:
            if isinstance(lr_schedule, (str, bytes)):
                raise TypeError("lr_schedule must be a sequence of numbers or a callable")
            try:
                lr_values = tuple(float(value) for value in lr_schedule)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "lr_schedule must be a sequence of numbers or a callable"
                ) from exc
            if not lr_values:
                raise ValueError("lr_schedule must not be empty")
            if not all(math.isfinite(value) for value in lr_values):
                raise ValueError("lr_schedule values must be finite")
            self._lr_callable = None
            self._lr_values = lr_values

        self._step = 0
        self._lr_position = 0
        self._reports: dict[str, _Payload] = {}
        self._failed_reason: str | None = None
        self._history: list[StepResult] = []
        # 可重入锁：lr_schedule 回调在持有锁时被调用，若它顺手读取
        # coordinator 的某个属性不应自锁死。
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # 只读状态
    # ------------------------------------------------------------------ #

    @property
    def parameters(self) -> tuple[float, ...]:
        """当前参数快照。"""
        with self._lock:
            return tuple(self._params)

    @property
    def current_step(self) -> int:
        """尚未结算的步号。"""
        with self._lock:
            return self._step

    @property
    def lr_position(self) -> int:
        """已成功更新的次数，也是下一次更新读取学习率的位置。"""
        with self._lock:
            return self._lr_position

    @property
    def history(self) -> tuple[StepResult, ...]:
        """所有已结算步的结果。"""
        with self._lock:
            return tuple(self._history)

    @property
    def pending_micro_batches(self) -> tuple[str, ...]:
        """当前步尚未收到的微批身份，按名单顺序。"""
        with self._lock:
            return tuple(
                mb_id for mb_id in self._order if mb_id not in self._reports
            )

    # ------------------------------------------------------------------ #
    # 上报
    # ------------------------------------------------------------------ #

    def submit(self, report: GradientReport) -> SubmitResult:
        """接收一条微批上报。

        返回值语义：

        * ``ACCEPTED``：首次收到该微批的贡献；
        * ``DUPLICATE``：同一微批的相同重传，只计一次；
        * ``CONFLICT``：同一微批的载荷不同，该步被标记为失败；
        * ``STEP_FAILED``：该步已被冲突判定为失败，后续上报被忽略；
        * ``STALE``：步号早于当前步的旧回执，不改变任何状态。

        指向未来步、未知微批、rank 不匹配或梯度维度不符的上报属于调用方
        错误，抛出异常且不改变状态。步号超前于当前步时，需要先结算当前步。
        """
        if not isinstance(report, GradientReport):
            raise TypeError("report must be a GradientReport instance")

        with self._lock:
            if report.step < self._step:
                return SubmitResult.STALE
            if report.step > self._step:
                raise FutureStepError(self._step, report.step)

            micro_batch = self._by_id.get(report.mb_id)
            if micro_batch is None:
                raise UnknownMicroBatchError(report.mb_id)
            if micro_batch.rank != report.rank:
                raise RankMismatchError(
                    report.mb_id, micro_batch.rank, report.rank
                )
            self._validate_gradient(report)

            if self._failed_reason is not None:
                # 该步已判定失败，后续上报不再参与结算，也不改变状态；
                # 结构性错误（未知微批、rank、维度）仍然照常暴露。
                return SubmitResult.STEP_FAILED

            payload = self._canonicalize(report)
            existing = self._reports.get(report.mb_id)
            if existing is not None:
                if existing == payload:
                    return SubmitResult.DUPLICATE
                self._failed_reason = (
                    f"conflicting retransmission for micro-batch {report.mb_id!r}"
                )
                return SubmitResult.CONFLICT

            self._reports[report.mb_id] = payload
            return SubmitResult.ACCEPTED

    # ------------------------------------------------------------------ #
    # 结算
    # ------------------------------------------------------------------ #

    def settle(self) -> StepResult:
        """结算当前步并推进步号。

        成功更新后参数变为 ``theta - lr * sum_i (tokens_i / total) * grad_i``，
        学习率位置加一。判定顺序：

        1. 曾出现冲突重传 → ``FAILED_CONFLICT``，立即失败，不必等名单收齐；
        2. 名单未收齐 → 抛 ``IncompleteStepError``，状态完全不变；
        3. 收齐后任一条上报 overflow → ``SKIPPED_OVERFLOW``；
        4. 收齐后整步有效 Token 数为 0 → ``SKIPPED_NO_TOKENS``。

        除第 2 种情况外都会推进步号；只有成功更新才推进学习率位置。若读取
        学习率失败（``LearningRateScheduleError``），该步保持未结算：参数、
        步号与学习率位置都不变，可配置好学习率后重试。
        """
        with self._lock:
            step = self._step

            if self._failed_reason is not None:
                return self._finalize(
                    status=StepStatus.FAILED_CONFLICT,
                    total_valid_tokens=self._total_tokens(),
                    lr_position=self._lr_position,
                    learning_rate=None,
                    weights=None,
                    reason=self._failed_reason,
                )

            missing = tuple(
                mb_id for mb_id in self._order if mb_id not in self._reports
            )
            if missing:
                raise IncompleteStepError(step, missing)

            total = self._total_tokens()
            if any(payload.overflow for payload in self._reports.values()):
                return self._finalize(
                    status=StepStatus.SKIPPED_OVERFLOW,
                    total_valid_tokens=total,
                    lr_position=self._lr_position,
                    learning_rate=None,
                    weights=None,
                    reason="at least one micro-batch reported overflow",
                )

            if total == 0:
                return self._finalize(
                    status=StepStatus.SKIPPED_NO_TOKENS,
                    total_valid_tokens=0,
                    lr_position=self._lr_position,
                    learning_rate=None,
                    weights=None,
                    reason="step has no valid tokens",
                )

            lr_position = self._lr_position
            learning_rate = self._learning_rate(lr_position)

            normalized = [0.0] * self._dim
            weights: dict[str, float] = {}
            for mb_id in self._order:
                payload = self._reports[mb_id]
                if payload.valid_tokens == 0:
                    weights[mb_id] = 0.0
                    continue
                weight = payload.valid_tokens / total
                weights[mb_id] = weight
                gradient = payload.gradient
                if gradient is None:  # 不可达：有 Token 且未溢出必然携带梯度
                    raise CoordinatorError(
                        f"internal invariant broken for micro-batch {mb_id!r}"
                    )
                for index, value in enumerate(gradient):
                    normalized[index] += weight * value

            updated = [
                parameter - learning_rate * value
                for parameter, value in zip(self._params, normalized)
            ]
            if not all(math.isfinite(value) for value in updated):
                raise CoordinatorError(
                    "parameter update produced a non-finite value; "
                    "the step was not settled"
                )

            self._params = updated
            self._lr_position = lr_position + 1
            return self._finalize(
                status=StepStatus.APPLIED,
                total_valid_tokens=total,
                lr_position=lr_position,
                learning_rate=learning_rate,
                weights=weights,
                reason=None,
            )

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #

    def _validate_gradient(self, report: GradientReport) -> None:
        if report.overflow or report.valid_tokens == 0:
            # 该微批没有数值贡献，梯度既不参与加权也不参与身份比较。
            return
        if report.gradient is None:
            raise ValueError(
                f"micro-batch {report.mb_id!r} must carry a gradient"
            )
        if len(report.gradient) != self._dim:
            raise ValueError(
                f"gradient for micro-batch {report.mb_id!r} has length "
                f"{len(report.gradient)}, expected {self._dim}"
            )
        if not all(math.isfinite(value) for value in report.gradient):
            raise ValueError(
                f"gradient for micro-batch {report.mb_id!r} has non-finite "
                f"components"
            )

    @staticmethod
    def _canonicalize(report: GradientReport) -> _Payload:
        if report.overflow or report.valid_tokens == 0:
            return _Payload(report.valid_tokens, report.overflow, None)
        return _Payload(report.valid_tokens, report.overflow, report.gradient)

    def _total_tokens(self) -> int:
        return sum(payload.valid_tokens for payload in self._reports.values())

    def _learning_rate(self, position: int) -> float:
        if self._lr_callable is not None:
            try:
                learning_rate = float(self._lr_callable(position))
            except Exception as exc:  # 边界处统一转换为协调器错误
                raise LearningRateScheduleError(
                    f"lr_schedule failed at position {position}"
                ) from exc
        else:
            lr_values = self._lr_values
            if lr_values is None:  # 不可达：构造时二选一
                raise CoordinatorError("internal invariant broken: no lr schedule")
            if position >= len(lr_values):
                raise LearningRateScheduleError(
                    f"lr_schedule has {len(lr_values)} entries, "
                    f"but position {position} is required"
                )
            learning_rate = lr_values[position]
        if not math.isfinite(learning_rate):
            raise LearningRateScheduleError(
                f"lr_schedule produced a non-finite learning rate at "
                f"position {position}"
            )
        return learning_rate

    def _finalize(
        self,
        *,
        status: StepStatus,
        total_valid_tokens: int,
        lr_position: int,
        learning_rate: float | None,
        weights: dict[str, float] | None,
        reason: str | None,
    ) -> StepResult:
        contributions = tuple(
            Contribution(
                mb_id=micro_batch.mb_id,
                rank=micro_batch.rank,
                valid_tokens=self._reports[micro_batch.mb_id].valid_tokens,
                weight=None if weights is None else weights[micro_batch.mb_id],
            )
            for micro_batch in self._roster
            if micro_batch.mb_id in self._reports
        )
        result = StepResult(
            step=self._step,
            status=status,
            total_valid_tokens=total_valid_tokens,
            lr_position=lr_position,
            learning_rate=learning_rate,
            parameters=tuple(self._params),
            contributions=contributions,
            reason=reason,
        )
        self._history.append(result)
        self._reports.clear()
        self._failed_reason = None
        self._step += 1
        return result

    def __repr__(self) -> str:  # 便于训练循环日志中识别当前进度
        with self._lock:
            step = self._step
            position = self._lr_position
        return (
            f"{type(self).__name__}(step={step}, "
            f"lr_position={position}, dim={self._dim})"
        )
