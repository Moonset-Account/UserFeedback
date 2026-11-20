"""固定大小页的磁盘表示。

页布局（大端）::

    +----------------+----------------+----------------+----------------------+
    | commit_seq (8) | payload_len(4) | crc32 (4)      | payload (变长，<=页容量)|
    +----------------+----------------+----------------+----------------------+

* ``commit_seq``：该页内容最后一次落盘所属的已提交事务号；未参与任何事务时为 0。
* ``crc32``：对 ``commit_seq``、``payload_len`` 与 ``payload`` 拼接字节做 CRC32，
  页头中的 crc 字段本身不参与计算。
* 页文件只序列化 ``页头 + payload``，不写填充字节，保证同内容字节级一致，
  从而支持恢复流程的幂等判断。
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

# uint64 commit_seq, uint32 payload_len, uint32 crc32
_PAGE_HEADER_FORMAT = ">QII"
PAGE_HEADER_SIZE = struct.calcsize(_PAGE_HEADER_FORMAT)


def page_crc(commit_seq: int, payload: bytes) -> int:
    """计算页（除 crc 字段外）的 CRC32 校验值。"""
    head = struct.pack(">QI", commit_seq, len(payload))
    return zlib.crc32(head + payload) & 0xFFFFFFFF


@dataclass(frozen=True)
class Page:
    """解析后的页内容。"""

    commit_seq: int
    payload: bytes

    def serialize(self, page_size: int) -> bytes:
        return build_page(self.commit_seq, self.payload, page_size)


def build_page(commit_seq: int, payload: bytes, page_size: int) -> bytes:
    """按页格式序列化一页，载荷超容量时抛出 ``ValueError``。"""
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload must be bytes-like")
    payload = bytes(payload)
    if commit_seq < 0 or commit_seq > 0xFFFFFFFFFFFFFFFF:
        raise ValueError("commit_seq out of uint64 range")
    capacity = page_size - PAGE_HEADER_SIZE
    if capacity < 0:
        raise ValueError("page_size smaller than page header")
    if len(payload) > capacity:
        raise ValueError(
            f"payload {len(payload)} bytes exceeds page capacity {capacity}"
        )
    crc = page_crc(commit_seq, payload)
    return struct.pack(_PAGE_HEADER_FORMAT, commit_seq, len(payload), crc) + payload


def parse_page(blob: bytes) -> Page:
    """解析并校验一页。

    长度不足、声明长度与实际不符或 CRC 校验失败时抛出 ``ValueError``。
    """
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise TypeError("page blob must be bytes-like")
    blob = bytes(blob)
    if len(blob) < PAGE_HEADER_SIZE:
        raise ValueError("page shorter than header")
    commit_seq, payload_len, stored_crc = struct.unpack(
        _PAGE_HEADER_FORMAT, blob[:PAGE_HEADER_SIZE]
    )
    if len(blob) != PAGE_HEADER_SIZE + payload_len:
        raise ValueError("declared payload length does not match blob size")
    payload = blob[PAGE_HEADER_SIZE:]
    actual_crc = page_crc(commit_seq, payload)
    if actual_crc != stored_crc:
        raise ValueError("page checksum mismatch")
    return Page(commit_seq=commit_seq, payload=payload)


def is_valid_page(blob: bytes, recovery_point: int | None = None) -> bool:
    """页是否校验正确；给定 ``recovery_point`` 时还要求提交号未越过恢复点。"""
    try:
        page = parse_page(blob)
    except (ValueError, TypeError):
        return False
    if recovery_point is not None and page.commit_seq > recovery_point:
        return False
    return True
