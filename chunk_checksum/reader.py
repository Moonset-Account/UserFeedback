"""旧对象读取抽象与内存实现。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import ByteRange


@runtime_checkable
class Reader(Protocol):
    """旧对象读取器协议：按半开区间 ``[start, end)`` 读取旧字节。

    实现方可以对接真实对象存储的 Range GET；本模块保证只在合成确有
    必要时才调用，且区间不会触及本次覆盖写入的新字节范围。
    """

    def read_range(self, start: int, end: int) -> bytes: ...


class InMemoryReader:
    """基于内存 bytes 的读取器，同时记录所有实际发生的读取。"""

    def __init__(self, data: bytes | bytearray | memoryview) -> None:
        self._data = bytes(data)
        self._performed: list[ByteRange] = []

    def read_range(self, start: int, end: int) -> bytes:
        if not isinstance(start, int) or not isinstance(end, int):
            raise TypeError("读取边界必须为整数")
        if start < 0 or end < start or end > len(self._data):
            raise ValueError(
                f"越界读取 [{start}, {end})，旧对象长度 {len(self._data)}"
            )
        self._performed.append(ByteRange(start, end))
        return self._data[start:end]

    @property
    def performed_reads(self) -> tuple[ByteRange, ...]:
        return tuple(self._performed)

    @property
    def bytes_read(self) -> int:
        return sum(r.length for r in self._performed)
