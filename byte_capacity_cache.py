"""按字节容量限制的缓存，带频率门槛的 LRU 淘汰与周期性频率折半。

规则（与题面一致）：
- 每次访问输入 (key, size, value)，单个键的大小固定。
- 每次访问先增加该键的频率（包括被拒绝的访问）。
- 命中：更新值并移到最近使用端。
- 未命中：
  - 单项大小超过总容量：直接拒绝。
  - 空间足够：直接进入。
  - 空间不足：按最久未用顺序取最小前缀凑够空位，
    仅当候选键频率 **严格大于** 拟淘汰项频率之和时才替换，否则拒绝，
    被拒绝时缓存内容与次序保持不变。
- 每满 W 次访问（访问计数为 W 的倍数时），本次访问结算完毕后，
  将所有键（含已被淘汰/被拒绝的键）的频率向下折半，再开始下一次访问。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Hashable, List


@dataclass(frozen=True)
class AccessResult:
    """单次访问的完整结算结果，可据此复算该次决策。"""

    access_index: int          # 从 1 开始的访问序号
    key: Hashable
    hit: bool                  # 是否命中
    admitted: bool             # 本次访问后该键是否在缓存中（命中视为 True）
    evicted_keys: List[Hashable] = field(default_factory=list)  # 按淘汰顺序
    cache_bytes: int = 0       # 本次访问结算后的缓存字节数
    key_frequency: int = 0     # 该键本次增频后、折半前的频率
    halved: bool = False       # 本次访问结算后是否发生了频率折半


class ByteCapacityCache:
    """按字节容量限制、带频率门槛淘汰的缓存。"""

    def __init__(self, capacity: int, window: int) -> None:
        if not isinstance(capacity, int) or capacity < 0:
            raise ValueError("capacity 必须为非负整数")
        if not isinstance(window, int) or window < 1:
            raise ValueError("window 必须为正整数")
        self._capacity = capacity
        self._window = window
        self._items: "OrderedDict[Hashable, tuple[int, Any]]" = OrderedDict()
        self._bytes = 0
        self._freq: dict[Hashable, int] = {}
        self._count = 0

    # ------------------------------------------------------------------ API
    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def window(self) -> int:
        return self._window

    @property
    def cache_bytes(self) -> int:
        """当前缓存占用的字节数。"""
        return self._bytes

    @property
    def access_count(self) -> int:
        """已结算的访问次数。"""
        return self._count

    def frequency(self, key: Hashable) -> int:
        """键的当前频率（未出现过的键为 0）。"""
        return self._freq.get(key, 0)

    def keys(self) -> List[Hashable]:
        """缓存内键，按从最久未用到最近使用的顺序。"""
        return list(self._items.keys())

    def get_value(self, key: Hashable, default: Any = None) -> Any:
        """读取缓存中的值（不改变频率与次序）。"""
        item = self._items.get(key)
        return default if item is None else item[1]

    def __contains__(self, key: Hashable) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)

    # -------------------------------------------------------------- 核心逻辑
    def access(self, key: Hashable, size: int, value: Any = None) -> AccessResult:
        """执行一次访问并返回结算结果。"""
        if not isinstance(size, int) or size < 0:
            raise ValueError("size 必须为非负整数")

        self._count += 1
        self._freq[key] = self._freq.get(key, 0) + 1
        key_freq = self._freq[key]

        hit = key in self._items
        admitted = False
        evicted: List[Hashable] = []

        if hit:
            stored_size, _ = self._items[key]
            if size != stored_size:
                raise ValueError(
                    f"键 {key!r} 的大小固定为 {stored_size}，本次输入 {size}"
                )
            self._items[key] = (stored_size, value)
            self._items.move_to_end(key)
            admitted = True
        elif size > self._capacity:
            # 超容量单项直接拒绝，缓存次序保持。
            admitted = False
        else:
            needed = self._bytes + size - self._capacity
            if needed <= 0:
                self._items[key] = (size, value)
                self._bytes += size
                admitted = True
            else:
                # 按最久未用顺序取最小前缀凑够空位。
                candidates: List[Hashable] = []
                freed = 0
                freq_sum = 0
                for k, (s, _) in self._items.items():
                    if freed >= needed:
                        break
                    candidates.append(k)
                    freed += s
                    freq_sum += self._freq[k]
                if freed >= needed and key_freq > freq_sum:
                    for k in candidates:
                        s, _ = self._items.pop(k)
                        self._bytes -= s
                        evicted.append(k)
                    self._items[key] = (size, value)
                    self._bytes += size
                    admitted = True
                # 否则拒绝：缓存内容与次序不变，仅频率已累积。

        halved = False
        if self._count % self._window == 0:
            for k in self._freq:
                self._freq[k] //= 2
            halved = True

        return AccessResult(
            access_index=self._count,
            key=key,
            hit=hit,
            admitted=admitted,
            evicted_keys=evicted,
            cache_bytes=self._bytes,
            key_frequency=key_freq,
            halved=halved,
        )
