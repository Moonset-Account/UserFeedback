"""帧目录：定界、校验和、对齐、可选字段的版本化定义。

线格式
======

两个数据帧版本共享同一前导定界符 ``AA 55`` 与长度字段，使得在版本未确认
前，同一批字节可能同时满足两个版本的形式约束（“可证实边界”并存）：

V1（旧版，无可选字段、无对齐）::

    AA 55 | LEN(1) | PAYLOAD(LEN) | CHECKSUM(1)
    CHECKSUM = sum8(LEN + PAYLOAD)          # 8 位累加和

V2（新版，尾部可选时间戳，4 字节对齐，CRC-8/CCITT 风格多项式 0x07）::

    AA 55 | LEN(1) | PAYLOAD(LEN) | TS(2, 小端) | CRC(1) | PAD(0..3, 0x00)
    CRC = crc8(LEN + PAYLOAD + TS)
    整帧总长（含定界符）对齐到 4 字节，PAD 必须为零

版本由独立的控制包宣布，控制包使用不会与数据定界符混淆的独立标记::

    C3 9C | VERSION(1) | CHECKSUM(1 = sum8(VERSION))

测点载荷编码（两个版本一致）：每个测点占 3 字节
``POINT_ID(1) + VALUE(2, 大端有符号)``，长度不是 3 的倍数时尾部残段在
解析结果中通过 ``payload_remainder`` 显式报告，不静默丢弃。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

#: 数据帧定界符（新旧版本共享的同前缀）。
FRAME_MARKER: Final[bytes] = b"\xaa\x55"
#: 版本宣布控制包定界符。
CONTROL_MARKER: Final[bytes] = b"\xc3\x9c"

#: 解析状态哨兵：当前缓冲不足以判定（等待更多字节）。
INCOMPLETE: Final[str] = "incomplete"
#: 解析状态哨兵：形式约束被破坏（长度/校验/对齐/填充）。
REJECTED: Final[str] = "rejected"


def sum8(data: bytes) -> int:
    """8 位累加和（V1 与控制包使用）。"""
    return sum(data) & 0xFF


def crc8(data: bytes) -> int:
    """CRC-8，多项式 0x07、初值 0x00、无反射、无异或（V2 使用）。"""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


@dataclass(frozen=True)
class ParseOutcome:
    """单次帧尝试解析的结构化结果。

    status:
        ``"ok"`` | :data:`INCOMPLETE` | :data:`REJECTED`
    end_offset:
        成功时为帧尾（不含定界符起点）的绝对偏移；不完整/拒绝时为
        ``None``（拒绝时调用方负责推进重同步，不依赖该值）。
    measurements:
        ``(point_id, value)`` 元组列表。
    timestamp:
        V2 可选时间戳；V1 恒为 ``None``。
    reject_reason:
        拒绝原因（仅 ``status == "rejected"``）。
    payload_remainder:
        载荷中凑不满一个测点的尾部字节数。
    """

    status: str
    end_offset: int | None = None
    measurements: list[tuple[int, int]] = field(default_factory=list)
    timestamp: int | None = None
    reject_reason: str | None = None
    payload_remainder: int = 0


@dataclass(frozen=True)
class VersionSpec:
    """单个帧版本的目录项。"""

    version: int
    checksum_name: str
    has_timestamp: bool
    alignment: int  # 1 表示不对齐

    def total_length(self, payload_len: int) -> int:
        """按版本规则计算整帧（含 2 字节定界符）总长。"""
        # 标记 2 + 长度 1 + 载荷 + 校验 1，另加 V2 的 2 字节时间戳。
        length = 2 + 1 + payload_len + 1 + (2 if self.has_timestamp else 0)
        if self.alignment > 1:
            pad = (-length) % self.alignment
            length += pad
        return length

    def try_parse(
        self, buf: bytes, marker_offset: int, *, absolute: bool = False
    ) -> ParseOutcome:
        """在 ``buf[marker_offset:]`` 处尝试按本版本解帧。

        ``absolute`` 为真时返回的 ``end_offset`` 以 ``buf`` 起点为基准
        （归并器内部使用）；否则以 ``marker_offset`` 为基准（编解码自测
        使用）。
        """
        base = marker_offset
        if len(buf) - base < 3:
            return ParseOutcome(INCOMPLETE)
        payload_len = buf[base + 2]
        total = self.total_length(payload_len)
        if len(buf) - base < total:
            return ParseOutcome(INCOMPLETE)

        covered_end = base + 3 + payload_len + (2 if self.has_timestamp else 0)
        checksum_index = covered_end
        checksum_byte = buf[checksum_index]
        covered = buf[base + 2 : covered_end]

        if self.checksum_name == "sum8":
            expected = sum8(covered)
        else:
            expected = crc8(covered)
        if checksum_byte != expected:
            return ParseOutcome(
                REJECTED,
                reject_reason=f"checksum_mismatch:{self.checksum_name}",
            )

        if self.alignment > 1:
            pad = total - (checksum_index + 1 - base)
            padding = buf[checksum_index + 1 : base + total]
            if len(padding) != pad or any(padding):
                return ParseOutcome(
                    REJECTED, reject_reason="alignment_or_padding_error"
                )

        payload = buf[base + 3 : base + 3 + payload_len]
        measurements: list[tuple[int, int]] = []
        usable = (len(payload) // 3) * 3
        for i in range(0, usable, 3):
            point_id = payload[i]
            value = int.from_bytes(payload[i + 1 : i + 3], "big", signed=True)
            measurements.append((point_id, value))

        timestamp: int | None = None
        if self.has_timestamp:
            timestamp = int.from_bytes(
                buf[base + 3 + payload_len : base + 3 + payload_len + 2],
                "little",
            )

        end_absolute = base + total if absolute else total
        return ParseOutcome(
            "ok",
            end_offset=end_absolute,
            measurements=measurements,
            timestamp=timestamp,
            payload_remainder=len(payload) - usable,
        )


class FrameCatalog:
    """帧目录：持有各版本规格，并提供控制包解析与多版本评估。"""

    def __init__(self, specs: dict[int, VersionSpec]):
        if not specs:
            raise ValueError("catalog must contain at least one version spec")
        self._specs = dict(specs)

    @classmethod
    def default(cls) -> "FrameCatalog":
        """题面规定的 V1/V2 默认目录。"""
        return cls(
            {
                1: VersionSpec(
                    version=1,
                    checksum_name="sum8",
                    has_timestamp=False,
                    alignment=1,
                ),
                2: VersionSpec(
                    version=2,
                    checksum_name="crc8",
                    has_timestamp=True,
                    alignment=4,
                ),
            }
        )

    @property
    def versions(self) -> tuple[int, ...]:
        return tuple(sorted(self._specs))

    def spec(self, version: int) -> VersionSpec:
        return self._specs[version]

    def evaluate_all(
        self, buf: bytes, marker_offset: int
    ) -> dict[int, ParseOutcome]:
        """在同一标记处按目录内所有版本尝试解帧（保留多个可证实边界）。"""
        return {
            version: spec.try_parse(buf, marker_offset, absolute=True)
            for version, spec in self._specs.items()
        }

    def parse_control_packet(
        self, buf: bytes, marker_offset: int
    ) -> tuple[str, int | None, int | None]:
        """解析控制包。

        返回 ``(status, version, end_offset)``，status 为
        ``"ok"`` | :data:`INCOMPLETE` | :data:`REJECTED`；拒绝原因包括
        ``unknown_version`` 与 ``checksum_mismatch:sum8``。
        """
        base = marker_offset
        if len(buf) - base < 4:
            return INCOMPLETE, None, None
        version = buf[base + 2]
        checksum_byte = buf[base + 3]
        if version not in self._specs:
            return REJECTED, None, None
        if sum8(buf[base + 2 : base + 3]) != checksum_byte:
            return REJECTED, None, None
        return "ok", version, base + 4
