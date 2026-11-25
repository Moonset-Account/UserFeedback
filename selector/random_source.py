"""调用方提供的可重放随机字节流。

选择器本身不调用系统随机源，所有随机性都来自本模块包装的字节流，
因此只要给定相同的字节和起始偏移，抽样过程即可逐字节重放。

支持三种来源：

1. ``bytes``/``bytearray``/``memoryview``：按下标读取，可任意定位；
2. 二进制文件对象（需提供 ``read``）：顺序读取，``seek`` 仅支持前移；
3. 产出 ``bytes`` 的可迭代对象：顺序读取，仅支持前移。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, BinaryIO

from .errors import RandomnessExhaustedError

_UINT64_SIZE = 8
_UINT64_MOD = 1 << 64


class _BytesReader:
    """基于 bytes-like 对象的读取器，支持任意绝对定位。"""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, n: int) -> bytes:
        end = self._pos + n
        if end > len(self._data):
            self._pos = len(self._data)
            raise RandomnessExhaustedError(
                f"随机字节流不足：需要 {n} 字节，仅剩 {len(self._data) - self._pos} 字节"
            )
        chunk = self._data[self._pos : end]
        self._pos = end
        return chunk

    def skip(self, n: int) -> None:
        end = self._pos + n
        if end > len(self._data):
            self._pos = len(self._data)
            raise RandomnessExhaustedError(
                f"随机字节流不足：需要跳过 {n} 字节，仅剩 {len(self._data) - self._pos} 字节"
            )
        self._pos = end

    def seek(self, pos: int) -> None:
        if not 0 <= pos <= len(self._data):
            raise RandomnessExhaustedError(
                f"定位位置 {pos} 超出字节流长度 {len(self._data)}"
            )
        self._pos = pos

    @property
    def position(self) -> int:
        return self._pos

    _pos = 0


class _FileReader:
    """基于二进制文件对象的顺序读取器，只允许前移。"""

    def __init__(self, fp: BinaryIO) -> None:
        self._fp = fp
        self._pos = 0

    def _read_exact(self, n: int) -> bytes:
        chunk = self._fp.read(n)
        if chunk is None or len(chunk) < n:
            got = 0 if chunk is None else len(chunk)
            self._pos += got
            raise RandomnessExhaustedError(
                f"随机字节流提前结束：需要 {n} 字节，实际得到 {got} 字节"
            )
        self._pos += n
        return chunk

    def read(self, n: int) -> bytes:
        return self._read_exact(n)

    def skip(self, n: int) -> None:
        remaining = n
        while remaining > 0:
            chunk = self._fp.read(min(remaining, 1 << 16))
            if not chunk:
                raise RandomnessExhaustedError(
                    f"随机字节流提前结束：还需跳过 {remaining} 字节"
                )
            self._pos += len(chunk)
            remaining -= len(chunk)

    def seek(self, pos: int) -> None:
        if pos < self._pos:
            raise RandomnessExhaustedError(
                f"顺序字节流只能前移：当前位置 {self._pos}，请求位置 {pos}"
            )
        self.skip(pos - self._pos)

    @property
    def position(self) -> int:
        return self._pos


class _IteratorReader:
    """基于 bytes 迭代器的顺序读取器，只允许前移。"""

    def __init__(self, iterator: Iterable[bytes]) -> None:
        self._it = iter(iterator)
        self._buf = bytearray()
        self._pos = 0  # 逻辑流中已消费的绝对字节数

    def _fill(self, need: int) -> None:
        while len(self._buf) < need:
            try:
                chunk = next(self._it)
            except StopIteration:
                return
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise TypeError(
                    f"随机流迭代器只能产出 bytes-like 数据，得到 {type(chunk).__name__}"
                )
            self._buf.extend(chunk)

    def read(self, n: int) -> bytes:
        self._fill(n)
        if len(self._buf) < n:
            short = len(self._buf)
            self._pos += short
            del self._buf[:short]
            raise RandomnessExhaustedError(
                f"随机字节流提前结束：需要 {n} 字节，仅剩 {short} 字节"
            )
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        self._pos += n
        return chunk

    def skip(self, n: int) -> None:
        remaining = n
        while remaining > 0:
            if self._buf:
                take = min(remaining, len(self._buf))
                del self._buf[:take]
                self._pos += take
                remaining -= take
                continue
            try:
                chunk = next(self._it)
            except StopIteration:
                raise RandomnessExhaustedError(
                    f"随机字节流提前结束：还需跳过 {remaining} 字节"
                ) from None
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise TypeError(
                    f"随机流迭代器只能产出 bytes-like 数据，得到 {type(chunk).__name__}"
                )
            take = min(remaining, len(chunk))
            self._buf.extend(chunk[take:])
            self._pos += take
            remaining -= take

    def seek(self, pos: int) -> None:
        if pos < self._pos:
            raise RandomnessExhaustedError(
                f"顺序字节流只能前移：当前位置 {self._pos}，请求位置 {pos}"
            )
        self.skip(pos - self._pos)

    @property
    def position(self) -> int:
        return self._pos


class RandomByteStream:
    """对外暴露的随机字节流。

    参数：
        source: ``bytes``/``bytearray``/``memoryview``、二进制文件对象，
            或产出 ``bytes`` 的可迭代对象。
    """

    def __init__(self, source: bytes | bytearray | memoryview | BinaryIO | Iterable[bytes]) -> None:
        self._reader = self._wrap(source)

    @staticmethod
    def _wrap(source: Any) -> Any:
        if isinstance(source, (bytes, bytearray, memoryview)):
            return _BytesReader(bytes(source))
        if hasattr(source, "read") and callable(source.read):
            return _FileReader(source)
        if isinstance(source, Iterable):
            return _IteratorReader(source)
        raise TypeError(
            "随机字节流来源必须是 bytes-like、二进制文件对象或 bytes 迭代器，"
            f"得到 {type(source).__name__}"
        )

    @property
    def offset(self) -> int:
        """已消耗的字节数（随机流上的逻辑位置）。"""
        return self._reader.position

    def read_exact(self, n: int) -> bytes:
        """精确读取 n 个字节；不足时抛出 :class:`RandomnessExhaustedError`。"""
        if n <= 0:
            raise ValueError(f"读取字节数必须为正整数，得到 {n}")
        return self._reader.read(n)

    def read_uint64(self) -> int:
        """读取 8 字节大端无符号整数，范围 [0, 2**64)。"""
        return int.from_bytes(self.read_exact(_UINT64_SIZE), "big", signed=False)

    def seek(self, offset: int) -> None:
        """定位到绝对字节偏移；顺序来源只允许前移。"""
        if offset < 0:
            raise ValueError(f"字节偏移不能为负，得到 {offset}")
        self._reader.seek(offset)

    def snapshot(self) -> dict:
        """可 JSON 序列化的状态快照（仅记录消耗位置）。"""
        return {"offset": self._reader.position}

    def restore(self, state: dict) -> None:
        """从 :meth:`snapshot` 的结果恢复消耗位置。"""
        self.seek(int(state["offset"]))

    @staticmethod
    def uint64_modulus() -> int:
        """随机整数的模数 2**64。"""
        return _UINT64_MOD
