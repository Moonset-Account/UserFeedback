"""序列的配置、持久化状态与核心分配逻辑。

设计要点（对应题面）：

- 序列可正向（``increment_by > 0``）或反向（``increment_by < 0``）走。
- 全局状态只持久化"已预留到的边界" :attr:`SequenceState.watermark`：
  它是最近一次预留批次的最后一个值；进程退出后会话内未消费的号码不再补发，
  允许出现空缺。
- 会话（:class:`SequenceSession`）通过 :meth:`_SequenceEntry.reserve_batch`
  一次预留一小批号码（至多 ``cache_size`` 个），再逐次 ``nextval`` 发放。
- 到达界限时：``cycle=False`` 抛出 :class:`SequenceExhaustedError`；
  ``cycle=True`` 绕回到起点，但绕回前要等当前已预留号段全部用完或被释放。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

from .exceptions import (
    SequenceConfigError,
    SequenceExhaustedError,
    SequenceSessionClosedError,
)

#: 64 位有符号整数范围（数据库序列的常见取值域）。
MIN_BIGINT = -(2**63)
MAX_BIGINT = 2**63 - 1


@dataclass(frozen=True)
class SequenceConfig:
    """序列的静态配置。

    :param name: 序列名称（目录内唯一）。
    :param start_value: 起点（也是绕回后的起始值）。
    :param increment_by: 步长，正数正向走、负数反向走，不允许为 0。
    :param min_value: 可取的最小值。
    :param max_value: 可取的最大值。
    :param cache_size: 每个会话单次预留的号码个数上限，必须 >= 1。
    :param cycle: 到达界限后是否绕回；``False`` 时到界即结束。
    """

    name: str
    start_value: int = 1
    increment_by: int = 1
    min_value: int = MIN_BIGINT
    max_value: int = MAX_BIGINT
    cache_size: int = 20
    cycle: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise SequenceConfigError("序列名称必须是非空字符串")
        if not isinstance(self.increment_by, int) or isinstance(self.increment_by, bool):
            raise SequenceConfigError("步长必须是整数")
        if self.increment_by == 0:
            raise SequenceConfigError("步长不能为 0")
        for field_name in ("start_value", "min_value", "max_value", "cache_size"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise SequenceConfigError(f"{field_name} 必须是整数")
        if self.min_value > self.max_value:
            raise SequenceConfigError("min_value 不能大于 max_value")
        if not self.min_value <= self.start_value <= self.max_value:
            raise SequenceConfigError("start_value 必须落在 [min_value, max_value] 内")
        if self.cache_size < 1:
            raise SequenceConfigError("cache_size 必须 >= 1")
        if not isinstance(self.cycle, bool):
            raise SequenceConfigError("cycle 必须是布尔值")

    @property
    def ascending(self) -> bool:
        """是否为正向序列。"""
        return self.increment_by > 0

    def validate_increment(self, increment_by: int) -> None:
        """校验候选新步长本身合法（非 0、为整数）。

        改步长后序列从当前 watermark 沿新步长继续，是否立刻撞界由
        :meth:`_SequenceEntry.reserve_batch` 在下次预留时按实际边界判断，
        因此这里不拿 start_value 做越界推断。
        """
        if not isinstance(increment_by, int) or isinstance(increment_by, bool):
            raise SequenceConfigError("步长必须是整数")
        if increment_by == 0:
            raise SequenceConfigError("步长不能为 0")


@dataclass
class SequenceState:
    """序列的持久化状态（保存/恢复的就是这部分）。

    :param watermark: 已预留到的边界——最近一次预留批次的最后一个值；
        新建序列时为"起点前一个值"（``start_value - increment_by``）。
    :param version: 状态版本号，每次管理变更（改步长/指定下一值）+1，
        用于识别持有旧号段的会话。
    :param reclaimed_batches: 累计因管理变更被收回的会话号段数量（可观测性）。
    """

    watermark: int
    version: int = 1
    reclaimed_batches: int = 0

    def to_dict(self) -> dict:
        return {
            "watermark": self.watermark,
            "version": self.version,
            "reclaimed_batches": self.reclaimed_batches,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SequenceState":
        try:
            watermark = int(data["watermark"])
            version = int(data["version"])
            reclaimed_batches = int(data["reclaimed_batches"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SequenceConfigError(f"状态文件格式非法：{exc}") from exc
        if version < 1:
            raise SequenceConfigError("状态文件中的 version 必须 >= 1")
        if reclaimed_batches < 0:
            raise SequenceConfigError("reclaimed_batches 不能为负")
        return cls(
            watermark=watermark,
            version=version,
            reclaimed_batches=reclaimed_batches,
        )


class _SequenceEntry:
    """单个序列的全部运行期状态与同步原语。

    持有该条目锁时，才允许读写 :attr:`state`、:attr:`held_ranges` 与会话缓存。
    """

    def __init__(self, config: SequenceConfig, state: Optional[SequenceState] = None) -> None:
        self.config = config
        if state is None:
            state = SequenceState(watermark=config.start_value - config.increment_by)
        self.state = state
        self.lock = threading.RLock()
        # token -> (low, high)，当前各会话已预留但尚未完全消费/释放的号段。
        self.held_ranges: dict[int, tuple[int, int]] = {}
        self._next_token = 1

    # ---- 号段持有登记 -------------------------------------------------

    def _register_hold(self, low: int, high: int) -> int:
        token = self._next_token
        self._next_token += 1
        self.held_ranges[token] = (low, high)
        return token

    def _release_hold(self, token: Optional[int]) -> None:
        if token is not None:
            self.held_ranges.pop(token, None)

    # ---- 预留 ----------------------------------------------------------

    def reserve_batch(self) -> tuple[Deque[int], int]:
        """预留一小批号码，返回 ``(号码队列, 持有令牌)``。

        在条目锁内调用。绕回（cycle）前必须保证没有任何会话仍持有旧号段，
        否则抛出 :class:`SequenceExhaustedError`，由会话层转译为"等当前号段
        用完或释放"的可重试结束信号。
        """
        cfg = self.config
        st = self.state
        step = cfg.increment_by
        remaining = cfg.cache_size
        batch: Deque[int] = deque()

        # 从当前 watermark 的下一个值开始。
        candidate = st.watermark + step

        while remaining > 0:
            if step > 0 and candidate > cfg.max_value:
                if not cfg.cycle:
                    break  # 非绕回：已取到的部分作为短批返回，下次调用再结束
                if batch:
                    break  # 先把当前短批发完，下一轮预留时再等待/绕回
                self._wait_until_ranges_released()
                candidate = cfg.start_value
                continue
            if step < 0 and candidate < cfg.min_value:
                if not cfg.cycle:
                    break
                if batch:
                    break
                self._wait_until_ranges_released()
                candidate = cfg.start_value
                continue

            batch.append(candidate)
            st.watermark = candidate
            candidate += step
            remaining -= 1

        if not batch:
            # 整批一个值都没取到：非绕回序列彻底结束，或绕回前仍有号段在用。
            if cfg.cycle:
                self._wait_until_ranges_released()  # 必然抛出等待信号
            raise SequenceExhaustedError(
                f"序列 {cfg.name!r} 已到达界限 "
                f"({'max_value' if step > 0 else 'min_value'})，且未配置绕回"
            )

        if step > 0:
            low, high = batch[0], batch[-1]
        else:
            low, high = batch[-1], batch[0]
        token = self._register_hold(low, high)
        return batch, token

    def _wait_until_ranges_released(self) -> None:
        """绕回前置检查：当前号段必须全部用完或释放，否则报"等待"信号。

        本原型为单进程同步模型，不阻塞等待，而是抛出
        :class:`SequenceExhaustedError`，调用方先把手中剩余号码用完（或显式
        :meth:`SequenceSession.close` 释放）后重试即可完成绕回。
        """
        if self.held_ranges:
            raise SequenceExhaustedError(
                f"序列 {self.config.name!r} 已到达界限，需等当前已预留号段全部"
                f"用完或释放后才能绕回；尚持有号段 {sorted(self.held_ranges.values())}"
            )

    # ---- 管理变更 ------------------------------------------------------

    def alter_increment(self, increment_by: int) -> None:
        """改步长：收回所有未消费号段，沿新步长从旧预留边界继续。"""
        self.config.validate_increment(increment_by)
        reclaimed = len(self.held_ranges)
        self.held_ranges.clear()
        # 不可变配置，重建一份只替换步长。
        self.config = SequenceConfig(
            name=self.config.name,
            start_value=self.config.start_value,
            increment_by=increment_by,
            min_value=self.config.min_value,
            max_value=self.config.max_value,
            cache_size=self.config.cache_size,
            cycle=self.config.cycle,
        )
        self.state.version += 1
        self.state.reclaimed_batches += reclaimed
        # watermark 保持不变：下一个值 = 旧预留边界 + 新步长。

    def restart_with(self, next_value: int) -> None:
        """显式指定下一值：收回所有未消费号段，watermark 落到 next_value 之前。"""
        if not isinstance(next_value, int) or isinstance(next_value, bool):
            raise SequenceConfigError("next_value 必须是整数")
        if not self.config.min_value <= next_value <= self.config.max_value:
            raise SequenceConfigError(
                "next_value 必须落在 [min_value, max_value] 内"
            )
        reclaimed = len(self.held_ranges)
        self.held_ranges.clear()
        # 下一个发放值正好是 next_value。
        self.state.watermark = next_value - self.config.increment_by
        self.state.version += 1
        self.state.reclaimed_batches += reclaimed

    def release(self, token: Optional[int]) -> None:
        """会话释放其当前号段（号段用完或会话关闭时调用）。"""
        self._release_hold(token)


class SequenceSession:
    """一个取号会话：预留小批号码，逐次 :meth:`nextval` 发放。

    会话不是线程独占对象——目录内部用锁保证跨线程调用安全；但同一个会话
    被多线程并发 ``nextval`` 时，号码发放顺序由锁串行化，值仍保证不重复。
    """

    def __init__(self, entry: _SequenceEntry) -> None:
        self._entry = entry
        self._cache: Deque[int] = deque()
        self._token: Optional[int] = None
        self._version = entry.state.version
        self._closed = False

    # ---- 内部辅助 ------------------------------------------------------

    def _invalidate_locked(self) -> bool:
        """管理变更后的惰性失效处理，返回是否发生了失效。"""
        if self._version != self._entry.state.version:
            # 旧版本预留的号段已在变更时被统一收回，这里只清理本地缓存。
            self._cache.clear()
            self._token = None
            self._version = self._entry.state.version
            return True
        return False

    # ---- 公开 API ------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def version(self) -> int:
        """会话当前绑定的序列版本号。"""
        return self._version

    @property
    def cached_count(self) -> int:
        """本地缓存中尚未发放的号码个数。"""
        with self._entry.lock:
            return len(self._cache)

    def nextval(self) -> int:
        """发放下一个号码。"""
        if self._closed:
            raise SequenceSessionClosedError("会话已关闭，不能继续取号")
        entry = self._entry
        with entry.lock:
            self._invalidate_locked()
            if not self._cache:
                entry.release(self._token)
                self._token = None
                self._cache, self._token = entry.reserve_batch()
            value = self._cache.popleft()
            if not self._cache:
                # 本批已全部消费，释放号段登记。
                entry.release(self._token)
                self._token = None
            return value

    def close(self) -> None:
        """关闭会话；未消费的预留号码直接丢弃，允许出现空缺。"""
        if self._closed:
            return
        with self._entry.lock:
            self._entry.release(self._token)
            self._token = None
            self._cache.clear()
            self._closed = True

    def __enter__(self) -> "SequenceSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
