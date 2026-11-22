"""训练步协调器：贡献收集、物理位置聚合与单步一次的动量更新。"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Tuple

from .storage import InvalidViewError, PhysicalStorage, View

__all__ = [
    "TrainingCoordinator",
    "StepResult",
    "InvalidContributionError",
]


class InvalidContributionError(ValueError):
    """贡献内容非法（形状不匹配、含 NaN 等）。"""


@dataclass
class StepResult:
    """一次 ``commit_step`` 的结果快照。"""

    step_id: int
    aborted: bool
    aborted_reason: str | None
    touched_positions: Tuple[int, ...]
    aggregated_gradients: Dict[int, float]
    momentum_after: Dict[int, float]
    weights_after: list[float]
    accepted_contribution_ids: Tuple[Any, ...]
    duplicate_contribution_ids: Tuple[Any, ...]
    rejected_contributions: int

    @property
    def updated_count(self) -> int:
        return len(self.touched_positions)


class TrainingCoordinator:
    """按训练步协调多个逻辑视图上的梯度贡献。

    语义保证
    --------
    1. 每份贡献在加入时把逻辑梯度逐元素映射到物理下标并做**快照**；
       之后视图重绑不影响已固定的贡献。
    2. 可重叠视图的贡献在同一物理位置上**相加**。
    3. 同一训练步内相同贡献 ID 只生效一次，重放不会重复累加。
    4. ``commit_step`` 对每个物理位置最多更新一次；未参与本步的
       权重与动量保持原样。
    5. 任何形状/映射非法都会让**整步作废**：权重与动量完全不动，
       ``begin_step`` 后后续合法输入仍可正常重跑。
    6. 所有修改方法内部加锁，可被多线程交错调用。

    动量公式（SGD+momentum，按物理位置保存）::

        v[p] = momentum * v[p] + g[p]
        w[p] = w[p] - lr * v[p]
    """

    def __init__(self, storage: PhysicalStorage, momentum: float = 0.9):
        if not isinstance(storage, PhysicalStorage):
            raise TypeError("storage 必须是 PhysicalStorage 实例")
        if isinstance(momentum, bool) or not isinstance(momentum, (int, float)):
            raise TypeError("momentum 必须是数值")
        momentum = float(momentum)
        if math.isnan(momentum):
            raise ValueError("momentum 不允许 NaN")
        if not 0.0 <= momentum < 1.0:
            raise ValueError("momentum 必须落在 [0, 1) 区间")

        self._storage = storage
        self._momentum_coef = momentum
        # 每个物理位置一份动量，初始为 0；未参与更新的位置永远保持原值。
        self._momentum: list[float] = [0.0] * storage.size
        self._lock = threading.RLock()
        self._begin_new_step(step_id=1)

    # ---- 内部：训练步状态 ----------------------------------------
    def _begin_new_step(self, step_id: int) -> None:
        self._step_id = step_id
        # 物理下标 -> 聚合梯度
        self._aggregated: Dict[int, float] = {}
        # 已被接受的贡献 ID（去重）
        self._seen_ids: set = set()
        # 已接受贡献的顺序记录（结果快照用）
        self._accepted_ids: list = []
        self._duplicate_ids: list = []
        self._rejected_count = 0
        self._poisoned = False
        self._poison_reason: str | None = None
        # 已固定贡献：(存储id, (物理下标...), (梯度值...))
        self._fixed: list[tuple[int, Tuple[int, ...], Tuple[float, ...]]] = []

    # ---- 公开属性 -------------------------------------------------
    @property
    def storage(self) -> PhysicalStorage:
        return self._storage

    @property
    def step_id(self) -> int:
        with self._lock:
            return self._step_id

    @property
    def momentum(self) -> list[float]:
        with self._lock:
            return list(self._momentum)

    @property
    def weights(self) -> list[float]:
        with self._lock:
            return self._storage.buffer

    def pending_aggregation(self) -> Dict[int, float]:
        """当前步尚未提交的聚合梯度（副本），便于观察/测试。"""
        with self._lock:
            return dict(self._aggregated)

    # ---- 梯度展平与校验 ------------------------------------------
    @staticmethod
    def _flatten(values: Iterable) -> list:
        out: list = []

        def walk(node):
            if isinstance(node, (list, tuple)):
                for child in node:
                    walk(child)
            else:
                out.append(node)

        walk(values)
        return out

    def _check_gradient_values(self, flat: list) -> list[float]:
        if not flat:
            raise InvalidContributionError("梯度不能为空")
        result = []
        for value in flat:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InvalidContributionError(
                    f"梯度元素必须是 int/float，得到 {type(value).__name__}"
                )
            value = float(value)
            if math.isnan(value):
                raise InvalidContributionError("梯度不允许 NaN")
            result.append(value)
        return result

    # ---- 贡献收集 -------------------------------------------------
    def add_contribution(self, view: View, gradient, contribution_id) -> bool:
        """登记一份按元素给出的梯度贡献。

        返回 ``True`` 表示被接受；``False`` 表示重复 ID 被忽略，或本步
        已经因非法输入作废。任何非法形状/映射都会把本步标记为作废，
        并原样抛出 :class:`InvalidViewError` /
        :class:`InvalidContributionError`。
        """
        with self._lock:
            if not isinstance(view, View):
                raise TypeError("view 必须是 View 实例")
            try:
                hash(contribution_id)
            except TypeError:
                raise InvalidContributionError("contribution_id 必须可哈希")

            if self._poisoned:
                # 整步已作废：不再接受任何贡献，直到 commit 后开新步。
                self._rejected_count += 1
                return False

            if contribution_id in self._seen_ids:
                # 同一份梯度回放：不重复累加，即使 payload/视图变了也忽略。
                self._duplicate_ids.append(contribution_id)
                return False

            # 视图必须绑定在本协调器管理的物理存储上。
            if view.storage is not self._storage:
                self._poisoned = True
                self._poison_reason = (
                    "视图绑定的存储与协调器管理的物理存储不一致"
                )
                raise InvalidViewError(self._poison_reason)

            # 1) 校验梯度形状（嵌套 list/tuple，按行优先展平）。
            expected = view.element_count
            try:
                flat = self._flatten(gradient)
                values = self._check_gradient_values(flat)
            except InvalidContributionError:
                self._poisoned = True
                self._poison_reason = "梯度内容非法"
                raise
            if len(values) != expected:
                self._poisoned = True
                self._poison_reason = (
                    f"梯度元素数 {len(values)} 与视图元素数 {expected} 不匹配"
                )
                raise InvalidContributionError(self._poison_reason)

            # 2) 解析并固定物理映射（此时快照；之后视图重绑不影响本贡献）。
            try:
                physical_indices = view.resolve()
            except InvalidViewError:
                self._poisoned = True
                self._poison_reason = "视图到物理存储的映射非法"
                raise

            # 3) 固定贡献并聚合到物理位置（重叠视图在此相加）。
            self._fixed.append(
                (id(view.storage), physical_indices, tuple(values))
            )
            for physical_index, grad_value in zip(physical_indices, values):
                self._aggregated[physical_index] = (
                    self._aggregated.get(physical_index, 0.0) + grad_value
                )

            self._seen_ids.add(contribution_id)
            self._accepted_ids.append(contribution_id)
            return True

    # ---- 提交 -----------------------------------------------------
    def commit_step(self, learning_rate: float = 0.01) -> StepResult:
        """结束当前步并打开下一步。

        若本步已作废，权重/动量完全不变，直接进入新的一步；
        否则对聚合到的每个物理位置各更新一次，未参与的位置保持原样。
        """
        with self._lock:
            if isinstance(learning_rate, bool) or not isinstance(
                learning_rate, (int, float)
            ):
                raise TypeError("learning_rate 必须是数值")
            learning_rate = float(learning_rate)
            if math.isnan(learning_rate) or learning_rate < 0:
                raise ValueError("learning_rate 必须是非负有限数")

            current_step = self._step_id

            if self._poisoned:
                result = StepResult(
                    step_id=current_step,
                    aborted=True,
                    aborted_reason=self._poison_reason,
                    touched_positions=(),
                    aggregated_gradients={},
                    momentum_after={},
                    weights_after=self._storage.buffer,
                    accepted_contribution_ids=tuple(self._accepted_ids),
                    duplicate_contribution_ids=tuple(self._duplicate_ids),
                    rejected_contributions=self._rejected_count,
                )
                self._begin_new_step(step_id=current_step + 1)
                return result

            aggregated = dict(self._aggregated)
            touched = tuple(sorted(aggregated))
            buffer = self._storage._buffer  # 同一模块内，直接操作物理缓冲

            # 每个物理位置只更新一次。
            for position in touched:
                grad = aggregated[position]
                new_momentum = (
                    self._momentum_coef * self._momentum[position] + grad
                )
                self._momentum[position] = new_momentum
                buffer[position] = buffer[position] - learning_rate * new_momentum

            result = StepResult(
                step_id=current_step,
                aborted=False,
                aborted_reason=None,
                touched_positions=touched,
                aggregated_gradients=aggregated,
                momentum_after={p: self._momentum[p] for p in touched},
                weights_after=list(buffer),
                accepted_contribution_ids=tuple(self._accepted_ids),
                duplicate_contribution_ids=tuple(self._duplicate_ids),
                rejected_contributions=self._rejected_count,
            )
            self._begin_new_step(step_id=current_step + 1)
            return result

    def begin_step(self) -> int:
        """显式放弃当前步的所有待提交内容并重新开步（step_id 递增）。"""
        with self._lock:
            current = self._step_id
            self._begin_new_step(step_id=current + 1)
            return self._step_id
