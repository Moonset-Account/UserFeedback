"""LABSMP v1 二进制测量样本解析（虚构格式）。

字节布局（小端）：
  magic    8 字节，固定 b"LABSMP01"
  count    uint32，采样点数
  values   count * float64，测量值序列
  meas_id  32 字节 UTF-8（不足 NUL 填充），附件内嵌测量标识
  不允许多余尾部字节，否则判定格式错误。
"""
from __future__ import annotations

import struct

from .errors import CollectorError, ERR_SAMPLE_FORMAT

MEAS_ID_FIELD = 32
HEADER_SIZE = 8 + 4


def parse_labsmp(data: bytes) -> dict:
    if not isinstance(data, (bytes, bytearray)):
        raise CollectorError(ERR_SAMPLE_FORMAT, "附件负载不是字节数据")
    data = bytes(data)
    if len(data) < HEADER_SIZE:
        raise CollectorError(ERR_SAMPLE_FORMAT, f"长度 {len(data)} 不足 LABSMP 头部 {HEADER_SIZE} 字节")
    if data[:8] != b"LABSMP01":
        raise CollectorError(ERR_SAMPLE_FORMAT, "magic 标记不是 LABSMP01")
    count = struct.unpack("<I", data[8:12])[0]
    expected = HEADER_SIZE + count * 8 + MEAS_ID_FIELD
    if len(data) != expected:
        raise CollectorError(
            ERR_SAMPLE_FORMAT,
            f"声明 {count} 个采样点，期望 {expected} 字节，实际 {len(data)} 字节（含尾部多余字节同样拒绝）",
        )
    values = list(struct.unpack(f"<{count}d", data[12:12 + count * 8]))
    id_field = data[12 + count * 8:12 + count * 8 + MEAS_ID_FIELD]
    meas_id = id_field.rstrip(b"\x00").decode("utf-8", errors="strict")
    if not meas_id:
        raise CollectorError(ERR_SAMPLE_FORMAT, "附件内嵌测量标识为空")
    return {"count": count, "values": values, "measurement_id": meas_id}
