"""独立随机位的来源与按需均匀抽样原语。

题面要求：随机源只能按需给独立随机位。本模块把所有随机性收敛到
``BitSource.getbit()`` 一个调用点，抽样算法（拒绝采样 / 位累积）
只在真正需要下一位时才向来源索取，绝不预先批量取位。
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class BitSource(Protocol):
    """随机位来源协议：每次调用返回一个独立、无偏的布尔位。"""

    def getbit(self) -> bool:
        ...


class SystemRandomBitSource:
    """基于 ``os.urandom`` 的默认安全随机源（CSPRNG）。

    每个字节在被需要时才向操作系统申请，并逐位对外吐出，
    不缓存多余的字节到对象生命周期之外。
    """

    def __init__(self) -> None:
        self._byte = 0
        self._remaining = 0

    def getbit(self) -> bool:
        if self._remaining == 0:
            self._byte = os.urandom(1)[0]
            self._remaining = 8
        bit = (self._byte & 1) == 1
        self._byte >>= 1
        self._remaining -= 1
        return bit


class ZeroBitSource:
    """永远返回 0 的确定来源，用于边界与可复现测试。"""

    calls = 0

    def getbit(self) -> bool:
        self.calls += 1
        return False


class SequenceBitSource:
    """按给定 0/1 序列依次供位，用尽后抛出 ``IndexError``。

    用于精确验证“按需取位”：测试可以断言消耗的位数与期望一致。
    """

    def __init__(self, bits) -> None:
        self._bits = [bool(b) for b in list(bits)]
        self._index = 0

    @property
    def consumed(self) -> int:
        return self._index

    def getbit(self) -> bool:
        if self._index >= len(self._bits):
            raise IndexError("SequenceBitSource exhausted")
        bit = self._bits[self._index]
        self._index += 1
        return bit


class LcgBitSource:
    """确定性 LCG 随机位源（仅用于测试，不可用于生产）。

    每次产出一位，整段位流可由种子完全复现。
    """

    def __init__(self, seed: int, modulus: int = 2**31,
                 multiplier: int = 1103515245, increment: int = 12345) -> None:
        self._state = seed & (modulus - 1)
        self._modulus = modulus
        self._multiplier = multiplier
        self._increment = increment
        self._byte = 0
        self._remaining = 0
        self.calls = 0

    def _next_byte(self) -> int:
        self._state = (self._multiplier * self._state + self._increment) % self._modulus
        return self._state & 0xFF

    def getbit(self) -> bool:
        if self._remaining == 0:
            self._byte = self._next_byte()
            self._remaining = 8
        bit = (self._byte & 1) == 1
        self._byte >>= 1
        self._remaining -= 1
        self.calls += 1
        return bit


def uniform_inside(n: int, source: BitSource) -> int:
    """以独立随机位均匀抽取 ``[0, n)`` 内的整数。

    采用经典拒绝采样：取 ``k = ceil(log2(n))`` 个位组成候选，
    候选落在 ``[0, n)`` 之外就整组丢弃重来。每个被接受的整数
    概率严格相等，且每一位都是在需要时才向 ``source`` 索取。

    :param n: 上界（不含），必须 >= 1。
    """
    if not isinstance(n, int) or n < 1:
        raise ValueError("n must be a positive integer")
    if n == 1:
        return 0
    # 表示 [0, n) 所需的最小位数：n=4 -> 2，n=3 -> 2，n=5 -> 3
    k = (n - 1).bit_length()
    while True:
        value = 0
        for _ in range(k):  # 每轮拒绝后整组 k 位重新索取
            value = (value << 1) | (1 if source.getbit() else 0)
        # n 为 2 的幂时 value 恒 < n，条件自然成立，不会发生拒绝
        if value < n:
            return value
