"""混合精度训练协调模块。

用小向量（list[float]）表示参数与动量。协调流程：

1. ``begin_step`` 固定本步的 rank 集合、微批数与损失缩放值，并分配单调
   递增的 epoch。
2. 各 rank 通过 ``report_grad`` 分批回传*已缩放*梯度与非有限标志。
3. 收齐后仅执行一次全局反缩放、合并与优化器推进。
4. 任一有效微批非有限 -> 整步跳过，保留参数与动量，下调缩放；
   连续成功达到阈值才上调缩放。
5. 成员退出 -> ``abort_step`` 撤销整步，保留原缩放与连续成功计数；
   旧 epoch 的迟到报告被拒绝，不会进入重试步。
6. 汇总完成后、优化器发布前写 journal，崩溃恢复时幂等重放。

仅使用标准库，状态以 JSON 原子写入 ``state_dir``。
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple


# --------------------------------------------------------------------------- #
# 异常类型
# --------------------------------------------------------------------------- #
class CoordinatorError(Exception):
    """协调器所有错误的基类。"""


class NoActiveStepError(CoordinatorError):
    """当前没有进行中的全局步。"""


class StepAlreadyActiveError(CoordinatorError):
    """上一步尚未结束（未收齐也未撤销）。"""


class StaleReportError(CoordinatorError):
    """迟到报告：epoch 与当前步不匹配，或步已结束。"""


class DuplicateReportError(CoordinatorError):
    """同一 (rank, microbatch) 重复上报。"""


class UnknownRankError(CoordinatorError):
    """上报者不在本步固定的 rank 集合内。"""


class BadGradientError(CoordinatorError):
    """梯度维度与参数不一致。"""


class NotInitializedError(CoordinatorError):
    """尚未初始化参数。"""


class AlreadyInitializedError(CoordinatorError):
    """参数已初始化（或已从持久化状态恢复）。"""


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CoordinatorConfig:
    learning_rate: float = 0.1
    momentum_beta: float = 0.9
    initial_scale: float = 8.0
    growth_interval: int = 2       # 连续成功多少步才上调缩放
    scale_backoff: float = 0.5     # 溢出时缩放乘以此因子
    scale_growth: float = 2.0      # 达到阈值时缩放乘以此因子
    min_scale: float = 1.0
    max_scale: float = 1024.0


# --------------------------------------------------------------------------- #
# 进行中的全局步（仅内存态；崩溃后由驱动层重试整步）
# --------------------------------------------------------------------------- #
class _ActiveStep:
    def __init__(self, epoch: int, step_id: int, ranks: Sequence[int],
                 num_microbatches: int, scale: float) -> None:
        self.epoch = epoch
        self.step_id = step_id
        self.ranks = frozenset(ranks)
        self.num_microbatches = num_microbatches
        self.scale = scale
        # (rank, microbatch_index) -> (scaled_grad, finite_flag)
        self.reports: Dict[Tuple[int, int], Tuple[List[float], bool]] = {}

    @property
    def expected_reports(self) -> int:
        return len(self.ranks) * self.num_microbatches

    @property
    def complete(self) -> bool:
        return len(self.reports) == self.expected_reports


# --------------------------------------------------------------------------- #
# 协调器
# --------------------------------------------------------------------------- #
class MixedPrecisionCoordinator:
    """混合精度训练协调器（SGD + 动量，动态损失缩放）。"""

    STATE_FILE = "state.json"
    JOURNAL_FILE = "journal.json"

    def __init__(
        self,
        config: Optional[CoordinatorConfig] = None,
        state_dir: str = ".",
        crash_hook: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        :param config: 协调配置；缺省用默认值。
        :param state_dir: 状态目录（state.json / journal.json）。
        :param crash_hook: 测试用钩子。若提供，在“汇总日志已落盘、优化器
            尚未发布”这一恢复点处调用，可用于注入崩溃。
        """
        self._config = config or CoordinatorConfig()
        self._state_dir = state_dir
        self._crash_hook = crash_hook
        os.makedirs(self._state_dir, exist_ok=True)

        self._params: Optional[List[float]] = None
        self._momentum: Optional[List[float]] = None
        self._train_step = 0
        self._scale = self._config.initial_scale
        self._success_streak = 0
        self._epoch = 0
        self._active: Optional[_ActiveStep] = None

        if os.path.exists(self._state_path):
            self._load_state()
        self._recover_journal()

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #
    @property
    def _state_path(self) -> str:
        return os.path.join(self._state_dir, self.STATE_FILE)

    @property
    def _journal_path(self) -> str:
        return os.path.join(self._state_dir, self.JOURNAL_FILE)

    @staticmethod
    def _atomic_write_json(path: str, obj: dict) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _persist_state(self) -> None:
        self._atomic_write_json(self._state_path, {
            "params": self._params,
            "momentum": self._momentum,
            "train_step": self._train_step,
            "scale": self._scale,
            "success_streak": self._success_streak,
            "epoch": self._epoch,
        })

    def _load_state(self) -> None:
        with open(self._state_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self._params = data["params"]
        self._momentum = data["momentum"]
        self._train_step = data["train_step"]
        self._scale = data["scale"]
        self._success_streak = data["success_streak"]
        self._epoch = data["epoch"]

    def _recover_journal(self) -> None:
        """恢复点：汇总已完成但优化器结果未必发布。

        journal 记录了目标 train_step；若持久态已到达该步号，说明提交
        已完成，仅清理 journal；否则按 journal 中的已反缩放梯度幂等
        重放优化器推进，保证恰好推进一次。
        """
        if not os.path.exists(self._journal_path):
            return
        with open(self._journal_path, "r", encoding="utf-8") as fh:
            journal = json.load(fh)
        if self._train_step < journal["target_train_step"]:
            self._apply_optimizer(journal["unscaled_grad"])
            self._train_step = journal["target_train_step"]
            self._success_streak = journal["target_success_streak"]
            self._scale = journal["target_scale"]
            self._persist_state()
        os.remove(self._journal_path)

    # ------------------------------------------------------------------ #
    # 只读状态
    # ------------------------------------------------------------------ #
    @property
    def params(self) -> Optional[List[float]]:
        return list(self._params) if self._params is not None else None

    @property
    def momentum(self) -> Optional[List[float]]:
        return list(self._momentum) if self._momentum is not None else None

    @property
    def train_step(self) -> int:
        """已发布的优化器步数。"""
        return self._train_step

    @property
    def scale(self) -> float:
        return self._scale

    @property
    def success_streak(self) -> int:
        return self._success_streak

    @property
    def current_epoch(self) -> int:
        return self._epoch

    @property
    def has_active_step(self) -> bool:
        return self._active is not None

    @property
    def pending_reports(self) -> int:
        return len(self._active.reports) if self._active else 0

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #
    def initialize(self, params: Sequence[float]) -> None:
        """设置初始参数，动量清零。已初始化（含恢复）时拒绝调用。"""
        if self._params is not None:
            raise AlreadyInitializedError("params already initialized")
        self._params = [float(p) for p in params]
        self._momentum = [0.0] * len(self._params)
        self._persist_state()

    # ------------------------------------------------------------------ #
    # 全局步生命周期
    # ------------------------------------------------------------------ #
    def begin_step(self, step_id: int, ranks: Sequence[int],
                   num_microbatches: int) -> int:
        """开始一个全局步，固定 rank 集合、微批数与本步缩放值。

        :returns: 本步 epoch；后续上报与撤销都必须携带它。
        """
        if self._params is None:
            raise NotInitializedError("call initialize() first")
        if self._active is not None:
            raise StepAlreadyActiveError(
                f"step {self._active.step_id} (epoch {self._active.epoch}) "
                "still active")
        if num_microbatches < 1:
            raise CoordinatorError("num_microbatches must be >= 1")
        if not ranks:
            raise CoordinatorError("ranks must be non-empty")
        self._epoch += 1
        self._active = _ActiveStep(
            epoch=self._epoch, step_id=step_id, ranks=ranks,
            num_microbatches=num_microbatches, scale=self._scale)
        # 持久化 epoch：崩溃重启后旧 epoch 的迟到报告仍被拒绝。
        self._persist_state()
        return self._epoch

    def report_grad(self, epoch: int, rank: int, microbatch_index: int,
                    scaled_grad: Sequence[float],
                    finite: bool = True) -> str:
        """回传一个微批的已缩放梯度。

        :returns: ``"collecting"`` / ``"committed"`` / ``"overflow_skipped"``
        """
        step = self._require_step(epoch)
        if rank not in step.ranks:
            raise UnknownRankError(f"rank {rank} not in this step")
        if not 0 <= microbatch_index < step.num_microbatches:
            raise CoordinatorError(
                f"microbatch_index {microbatch_index} out of range")
        key = (rank, microbatch_index)
        if key in step.reports:
            raise DuplicateReportError(f"duplicate report for {key}")
        grad = [float(g) for g in scaled_grad]
        if len(grad) != len(self._params):
            raise BadGradientError(
                f"grad dim {len(grad)} != param dim {len(self._params)}")
        # 非有限标志与梯度值双重校验：任一非有限都视为溢出。
        finite = bool(finite) and all(math.isfinite(g) for g in grad)
        step.reports[key] = (grad, finite)
        if not step.complete:
            return "collecting"
        return self._finalize(step)

    def abort_step(self, epoch: int) -> None:
        """成员退出等原因撤销整步。

        保留原缩放与连续成功计数（通信中断不是梯度溢出），参数、动量、
        训练步号均不变。之后可用 ``begin_step`` 以新 epoch 重试。
        """
        step = self._require_step(epoch)
        self._active = None
        # 状态未变，但落盘一次保证 epoch 语义清晰（epoch 在 begin 时已推进）。
        self._persist_state()

    # ------------------------------------------------------------------ #
    # 内部：收齐后的收尾
    # ------------------------------------------------------------------ #
    def _require_step(self, epoch: int) -> _ActiveStep:
        if self._active is None:
            raise NoActiveStepError("no active step")
        if epoch != self._active.epoch:
            raise StaleReportError(
                f"report for epoch {epoch}, active epoch is "
                f"{self._active.epoch}")
        return self._active

    def _finalize(self, step: _ActiveStep) -> str:
        self._active = None  # 收齐即封闭，之后的同 epoch 报告一律拒绝
        grads = [step.reports[key] for key in sorted(step.reports)]
        if any(not finite for _, finite in grads):
            # 溢出：整步跳过，保留参数与动量，下调缩放，清零连续成功计数。
            self._scale = max(self._config.min_scale,
                              self._scale * self._config.scale_backoff)
            self._success_streak = 0
            self._persist_state()
            return "overflow_skipped"

        # 全局只反缩放一次：sum(g) / (scale * 微批总数)。
        count = len(grads)
        inv = 1.0 / (step.scale * count)
        merged = [sum(g[i] for g, _ in grads) * inv
                  for i in range(len(self._params))]

        target_train_step = self._train_step + 1
        target_streak = self._success_streak + 1
        target_scale = self._scale
        if target_streak >= self._config.growth_interval:
            target_scale = min(self._config.max_scale,
                               self._scale * self._config.scale_growth)
            target_streak = 0

        # 恢复点：先落 journal，再发布优化器结果。
        self._atomic_write_json(self._journal_path, {
            "epoch": step.epoch,
            "step_id": step.step_id,
            "unscaled_grad": merged,
            "target_train_step": target_train_step,
            "target_success_streak": target_streak,
            "target_scale": target_scale,
        })
        if self._crash_hook is not None:
            self._crash_hook()

        self._apply_optimizer(merged)
        self._train_step = target_train_step
        self._success_streak = target_streak
        self._scale = target_scale
        self._persist_state()
        os.remove(self._journal_path)
        return "committed"

    def _apply_optimizer(self, grad: Sequence[float]) -> None:
        """SGD + 动量：m = beta*m + g；p = p - lr*m。"""
        beta = self._config.momentum_beta
        lr = self._config.learning_rate
        assert self._params is not None and self._momentum is not None
        for i in range(len(self._params)):
            self._momentum[i] = beta * self._momentum[i] + grad[i]
            self._params[i] -= lr * self._momentum[i]
