"""EXPB —— 本项目虚构的实验记录容器格式（Experiment Bundle v1）。

布局（全部为字节序列，多字节整数使用大端序）::

    MAGIC                       b"EXPB1\\n"               6 字节
    name_len                    uint8                    1 字节
    object_name                 UTF-8                    name_len 字节
    record_count                uint16                   2 字节
    payload_len                 uint64                   8 字节
    payload                     若干帧，共 payload_len 字节
    footer                      b"\\n#EXPB-END SHA256=" + 64 位十六进制摘要 + b"\\n"

payload 中每帧记录::

    frame_len                   uint32                   4 字节
    record_json                 UTF-8 JSON，共 frame_len 字节

SHA-256 摘要覆盖 footer 之前的全部字节（即魔数到 payload 结束）。

采集器只能在区间覆盖完整、文件长度与声明长度一致后调用解析器；任何魔数、
帧结构、字段、长度或摘要不符都会抛出 ``ExpbError``，损坏内容不会产生结果。
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from typing import Any, Dict, List

MAGIC = b"EXPB1\n"
FOOTER_PREFIX = b"\n#EXPB-END SHA256="
FOOTER_SUFFIX = b"\n"
HEX_DIGEST_LEN = 64
# footer = 前缀 + 64 位摘要 + 换行
FOOTER_LEN = len(FOOTER_PREFIX) + HEX_DIGEST_LEN + len(FOOTER_SUFFIX)

# 记录字段的合法范围（虚构实验协议的一部分）
GROUPS = ("A", "B", "C")
TEMP_RANGE = (-50.0, 150.0)
PRESSURE_RANGE = (0.0, 10.0)
MEASURED_RANGE = (-10000.0, 10000.0)


class ExpbError(ValueError):
    """EXPB 对象结构或校验和不合法。"""


@dataclass
class ExperimentObject:
    object_name: str
    records: List[Dict[str, Any]]
    sha256: str
    size: int

    @property
    def summary(self) -> Dict[str, Any]:
        """从记录真实计算的摘要（不读取任何外部真值）。"""
        temps = [float(r["temperature"]) for r in self.records]
        groups: Dict[str, int] = {}
        measured_sum = 0.0
        checked = 0
        for r in self.records:
            groups[r["group"]] = groups.get(r["group"], 0) + 1
            measured_sum += float(r["measured"])
            if r["checked"]:
                checked += 1
        return {
            "object_name": self.object_name,
            "record_count": len(self.records),
            "groups": groups,
            "temperature_min": min(temps),
            "temperature_max": max(temps),
            "temperature_avg": round(sum(temps) / len(temps), 6),
            "measured_sum": round(measured_sum, 6),
            "checked_count": checked,
            "sha256": self.sha256,
            "size": self.size,
        }


def _check_record(raw: bytes, index: int) -> Dict[str, Any]:
    try:
        rec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExpbError(f"第 {index} 条记录不是合法 UTF-8 JSON: {exc}") from exc
    required = ("id", "group", "temperature", "pressure", "measured", "checked", "note")
    for key in required:
        if key not in rec:
            raise ExpbError(f"第 {index} 条记录缺少字段 {key}")
    if not isinstance(rec["id"], int) or rec["id"] != index:
        raise ExpbError(f"第 {index} 条记录 id 不连续或类型错误")
    if rec["group"] not in GROUPS:
        raise ExpbError(f"第 {index} 条记录分组非法: {rec['group']!r}")
    for key, (lo, hi) in (
        ("temperature", TEMP_RANGE),
        ("pressure", PRESSURE_RANGE),
        ("measured", MEASURED_RANGE),
    ):
        value = rec[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ExpbError(f"第 {index} 条记录 {key} 不是数值")
        if not lo <= float(value) <= hi:
            raise ExpbError(f"第 {index} 条记录 {key}={value} 超出协议范围")
    if not isinstance(rec["checked"], bool):
        raise ExpbError(f"第 {index} 条记录 checked 不是布尔值")
    if not isinstance(rec["note"], str):
        raise ExpbError(f"第 {index} 条记录 note 不是字符串")
    return rec


def parse_expb(data: bytes) -> ExperimentObject:
    """解析并强校验一个完整的 EXPB 字节对象。"""
    if not isinstance(data, bytes):
        raise ExpbError("解析输入必须是 bytes")
    total = len(data)
    if total < len(MAGIC) + 1 + 2 + 8 + FOOTER_LEN:
        raise ExpbError("对象长度小于最小合法布局")
    if not data.startswith(MAGIC):
        raise ExpbError("魔数不匹配，不是 EXPB1 对象")

    pos = len(MAGIC)
    name_len = data[pos]
    pos += 1
    if total < pos + name_len + 2 + 8 + FOOTER_LEN:
        raise ExpbError("头部长度字段超出对象边界")
    try:
        object_name = data[pos : pos + name_len].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExpbError(f"对象名不是合法 UTF-8: {exc}") from exc
    pos += name_len
    record_count, payload_len = struct.unpack_from(">HQ", data, pos)
    pos += 10

    footer_start = pos + payload_len
    expected_size = footer_start + FOOTER_LEN
    if expected_size != total:
        raise ExpbError(
            f"声明 payload_len={payload_len} 推算总长度 {expected_size}，实际 {total}"
        )

    footer = data[footer_start:]
    if not footer.startswith(FOOTER_PREFIX) or not footer.endswith(FOOTER_SUFFIX):
        raise ExpbError("footer 边界标记不匹配")
    digest_hex = footer[len(FOOTER_PREFIX) : len(footer) - len(FOOTER_SUFFIX)]
    if len(digest_hex) != HEX_DIGEST_LEN:
        raise ExpbError("摘要长度不是 64 位十六进制")
    try:
        digest_text = digest_hex.decode("ascii")
        int(digest_text, 16)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExpbError("摘要不是合法十六进制") from exc

    actual = hashlib.sha256(data[:footer_start]).hexdigest()
    if actual != digest_text:
        raise ExpbError("SHA-256 校验失败：对象字节与摘要不一致")

    records: List[Dict[str, Any]] = []
    end = footer_start
    cursor = pos
    while cursor < end:
        if end - cursor < 4:
            raise ExpbError("payload 末尾残留不足 4 字节，帧截断")
        (frame_len,) = struct.unpack_from(">I", data, cursor)
        cursor += 4
        if frame_len == 0 or cursor + frame_len > end:
            raise ExpbError("帧长度为 0 或超出 payload 边界")
        records.append(_check_record(data[cursor : cursor + frame_len], len(records)))
        cursor += frame_len
    if cursor != end:
        raise ExpbError("帧解析未精确结束于 payload 边界")
    if len(records) != record_count:
        raise ExpbError(
            f"头部声明 {record_count} 条记录，实际解析出 {len(records)} 条"
        )
    return ExperimentObject(object_name, records, digest_text, total)


def parse_file(path: str) -> ExperimentObject:
    with open(path, "rb") as fh:
        return parse_expb(fh.read())
