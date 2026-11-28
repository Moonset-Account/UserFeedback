"""线格式编码辅助：供调用端与测试合成遥测字节流。

本模块只负责“按目录规则造字节”，不包含任何归并状态机逻辑。
"""

from __future__ import annotations

from .catalog import CONTROL_MARKER, FRAME_MARKER, VersionSpec, crc8, sum8


def encode_measurement(point_id: int, value: int) -> bytes:
    """编码单个测点：``POINT_ID(1) + VALUE(2, 大端有符号)``。"""
    if not 0 <= point_id <= 0xFF:
        raise ValueError("point_id must fit in one byte")
    if not -(1 << 15) <= value < (1 << 15):
        raise ValueError("value must be a signed 16-bit integer")
    return bytes((point_id,)) + int(value).to_bytes(2, "big", signed=True)


def encode_frame(spec: VersionSpec, payload: bytes, timestamp: int = 0) -> bytes:
    """按版本规格编码一个完整数据帧。"""
    if not 0 <= len(payload) <= 0xFF:
        raise ValueError("payload length must fit in one byte")
    if spec.has_timestamp and not 0 <= timestamp <= 0xFFFF:
        raise ValueError("timestamp must be an unsigned 16-bit integer")

    head = FRAME_MARKER + bytes((len(payload),)) + payload
    if not spec.has_timestamp:
        return head + bytes((sum8(head[2:]),))

    body = head + timestamp.to_bytes(2, "little")
    frame = body + bytes((crc8(body[2:]),))
    if spec.alignment > 1:
        frame += b"\x00" * ((-len(frame)) % spec.alignment)
    return frame


def build_control_packet(version: int) -> bytes:
    """构造版本宣布控制包 ``C3 9C | VERSION | sum8(VERSION)``。"""
    if not 0 <= version <= 0xFF:
        raise ValueError("version must fit in one byte")
    version_byte = bytes((version,))
    return CONTROL_MARKER + version_byte + bytes((sum8(version_byte),))


def build_ambiguous_v1_v2(
    catalog, point_id: int = 0x10, value: int = -250
) -> bytes:
    """构造跨版本同前缀样例：同一字节串同时是合法 V1 帧与合法 V2 帧。

    构造依据（两版本共享 ``AA 55 | LEN | PAYLOAD`` 前缀，单测点 LEN=3）：

    * V1 帧在索引 6 处要求 ``CHECKSUM = sum8(LEN + PAYLOAD)``，帧在该
      字节后结束（整帧 7 字节）；
    * V2 把索引 6、7 解释为小端时间戳，即索引 6 = 时间戳高字节。因此
      时间戳高字节被 V1 校验唯一固定为 ``sum8(LEN + PAYLOAD)``；
    * V2 的 CRC-8 覆盖 ``LEN + PAYLOAD + TS``，时间戳低字节通过暴力枚举
      0..255 反解出使 CRC 恰好等于线上下一个字节位置的取值；随后 V2 帧
      补零对齐到 4 字节（9 -> 12 字节）。

    返回的字节串在 :class:`~frame_merger.catalog.FrameCatalog` 的
    ``evaluate_all`` 下对 V1、V2 都产出 ``"ok"``，但帧尾不同
    （V1 7 字节、V2 12 字节），因此版本确认前两个边界必须同时保留。
    """
    v1 = catalog.spec(1)
    v2 = catalog.spec(2)
    payload = encode_measurement(point_id, value)
    ts_high = sum8(bytes((len(payload),)) + payload)

    low_byte: int | None = None
    for candidate in range(256):
        timestamp = (ts_high << 8) | candidate
        head = FRAME_MARKER + bytes((len(payload),)) + payload
        body = head + timestamp.to_bytes(2, "little")
        checksum = crc8(body[2:])
        frame = body + bytes((checksum,))
        frame += b"\x00" * ((-len(frame)) % v2.alignment)
        if v1.try_parse(frame, 0).status == "ok":
            low_byte = candidate
            break

    if low_byte is None:
        raise AssertionError("no timestamp low byte satisfies both versions")

    timestamp = (ts_high << 8) | low_byte
    frame = encode_frame(v2, payload, timestamp=timestamp)

    # 自证：同一字节串必须同时通过两个版本的解析器，且 V1 帧尾（索引 6）
    # 正好落在 V2 时间戳高字节上。
    outcome_v1 = v1.try_parse(frame, 0)
    outcome_v2 = v2.try_parse(frame, 0)
    if outcome_v1.status != "ok" or outcome_v2.status != "ok":
        raise AssertionError("ambiguous sample failed self-validation")
    if outcome_v1.end_offset == outcome_v2.end_offset:
        raise AssertionError("ambiguous sample must have distinct frame ends")
    if frame[6] != ts_high:
        raise AssertionError("ambiguous sample layout invariant broken")
    return frame
