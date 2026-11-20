"""追加日志（journal）的帧格式、写入器与读取器。

帧布局（全部大端）::

    +----------+-----------+-----------+----------+------------------+----------+
    | magic(4) | type (1)  | length(4) | seq (8)  | body (length 字节) | crc32(4)|
    +----------+-----------+-----------+----------+------------------+----------+

固定头部 17 字节，尾部 CRC 4 字节。``seq`` 对写记录是事务号，对提交记录是该
事务的提交号。

记录类型：

* ``WRITE(1)``  body = ``page_id(8) offset(8) replacement(N)``，帧头 seq=事务号
* ``COMMIT(2)`` body = ``transaction_id(8)``，帧头 seq=该事务的提交号

读取规则（与恢复需求对应）：

* 日志末尾出现任何读不满一条完整帧的情况，视为末条欠字节，记录截断位置，
  该欠字节部分及其后内容全部丢弃，前面的完整记录照常返回。
* 中段完整记录出现 magic 错误、长度字段非法或 CRC 不匹配，抛出
  :class:`JournalCorrupt`，其中携带坏记录在日志中的字节偏移。
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import BinaryIO

JOURNAL_MAGIC = b"JRnL"
WRITE = 1
COMMIT = 2

_JOURNAL_HEADER_FORMAT = ">4sBIQ"
_JOURNAL_HEADER_SIZE = struct.calcsize(_JOURNAL_HEADER_FORMAT)  # 17
_CRC_SIZE = 4
_MAX_BODY_LENGTH = 64 * 1024 * 1024  # 64 MiB 防御性上限，防止损坏长度撑爆内存

# WRITE body: uint64 page_id, uint64 offset, 其余为替换字节
_WRITE_BODY_PREFIX = ">QQ"
_WRITE_BODY_PREFIX_SIZE = struct.calcsize(_WRITE_BODY_PREFIX)


class JournalCorrupt(Exception):
    """日志中段的一条完整（或 magic 可读但内容非法）记录校验失败。"""

    def __init__(self, position: int, reason: str):
        super().__init__(f"journal corrupt at byte {position}: {reason}")
        self.position = position
        self.reason = reason


@dataclass(frozen=True)
class TruncatedJournal:
    """末条欠字节的描述信息。"""

    position: int
    """第一条不完整记录开始的字节偏移，也就是可保留内容的长度。"""

    missing: int
    """相对于一条完整帧仍缺少的字节数；无法确定帧长（头部即欠字节）时为 -1。"""


@dataclass(frozen=True)
class WriteRecord:
    """事务内的一条页补丁。"""

    transaction_id: int
    page_id: int
    offset: int
    replacement: bytes
    position: int
    """该记录在日志中的字节偏移，用于错误定位。"""


@dataclass(frozen=True)
class CommitRecord:
    """事务提交记录。``commit_seq`` 即该事务的提交号。"""

    transaction_id: int
    commit_seq: int
    position: int


def _record_crc(kind: int, length: int, seq: int, body: bytes) -> int:
    head = struct.pack(">BIQ", kind, length, seq)
    return zlib.crc32(head + body) & 0xFFFFFFFF


class JournalWriter:
    """向二进制流顺序追加日志记录。"""

    def __init__(self, stream: BinaryIO):
        self._stream = stream

    def write(
        self,
        transaction_id: int,
        page_id: int,
        offset: int,
        replacement: bytes,
    ) -> int:
        """追加一条 WRITE 记录，返回记录起始字节偏移。"""
        if not isinstance(replacement, (bytes, bytearray, memoryview)):
            raise TypeError("replacement must be bytes-like")
        replacement = bytes(replacement)
        body = struct.pack(_WRITE_BODY_PREFIX, page_id, offset) + replacement
        # WRITE 帧头 seq 位置承载事务号。
        return self._emit(WRITE, transaction_id, body, transaction_id)

    def commit(self, transaction_id: int, commit_seq: int) -> int:
        """追加一条 COMMIT 记录，返回记录起始字节偏移。"""
        if commit_seq <= 0:
            raise ValueError("commit_seq must be positive")
        # COMMIT 帧头 seq 位置承载提交号；事务号编码在 8 字节 body 中。
        body = struct.pack(">Q", transaction_id)
        return self._emit(COMMIT, commit_seq, body, transaction_id)

    def _emit(
        self,
        kind: int,
        frame_seq: int,
        body: bytes,
        transaction_id: int,
    ) -> int:
        length = len(body)
        if length > _MAX_BODY_LENGTH:
            raise ValueError("journal record body too large")
        start = self._stream.tell()
        header = struct.pack(
            _JOURNAL_HEADER_FORMAT, JOURNAL_MAGIC, kind, length, frame_seq
        )
        crc = _record_crc(kind, length, frame_seq, body)
        self._stream.write(header)
        self._stream.write(body)
        self._stream.write(struct.pack(">I", crc))
        return start


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_journal(stream: BinaryIO):
    """顺序读取整个日志。

    返回 ``(writes, commits, truncated)``：

    * ``writes``：:class:`WriteRecord` 列表，保持日志原始次序；
    * ``commits``：:class:`CommitRecord` 列表，保持日志原始次序；
    * ``truncated``：无欠字节时为 ``None``，否则为 :class:`TruncatedJournal`。

    遇到中段损坏抛出 :class:`JournalCorrupt`，调用方据此停止发布。
    """
    writes: list[WriteRecord] = []
    commits: list[CommitRecord] = []
    truncated: TruncatedJournal | None = None

    while True:
        start = stream.tell()
        header = _read_exact(stream, _JOURNAL_HEADER_SIZE)
        if len(header) == 0:
            break  # 干净的 EOF
        if len(header) < _JOURNAL_HEADER_SIZE:
            # 头部都读不满：无法知道帧长，missing 标记为 -1
            truncated = TruncatedJournal(position=start, missing=-1)
            break
        magic, kind, length, seq = struct.unpack(_JOURNAL_HEADER_FORMAT, header)
        if magic != JOURNAL_MAGIC:
            raise JournalCorrupt(start, "bad magic")
        if length > _MAX_BODY_LENGTH:
            raise JournalCorrupt(start, f"body length {length} exceeds limit")

        body = _read_exact(stream, length)
        crc_blob = _read_exact(stream, _CRC_SIZE)
        if len(body) < length or len(crc_blob) < _CRC_SIZE:
            # 末条欠字节：完整帧需要 header + length + 4 字节
            have = len(body) + len(crc_blob)
            want = length + _CRC_SIZE
            truncated = TruncatedJournal(
                position=start, missing=want - have
            )
            break

        (stored_crc,) = struct.unpack(">I", crc_blob)
        actual_crc = _record_crc(kind, length, seq, body)
        if actual_crc != stored_crc:
            raise JournalCorrupt(start, "record checksum mismatch")

        if kind == WRITE:
            if length < _WRITE_BODY_PREFIX_SIZE:
                raise JournalCorrupt(start, "write record body too short")
            page_id, offset = struct.unpack(
                _WRITE_BODY_PREFIX, body[:_WRITE_BODY_PREFIX_SIZE]
            )
            replacement = body[_WRITE_BODY_PREFIX_SIZE:]
            writes.append(
                WriteRecord(
                    transaction_id=seq,
                    page_id=page_id,
                    offset=offset,
                    replacement=replacement,
                    position=start,
                )
            )
        elif kind == COMMIT:
            if length != 8:
                raise JournalCorrupt(start, "commit record body must be 8 bytes")
            (transaction_id,) = struct.unpack(">Q", body)
            commits.append(
                CommitRecord(
                    transaction_id=transaction_id,
                    commit_seq=seq,
                    position=start,
                )
            )
        else:
            raise JournalCorrupt(start, f"unknown record type {kind}")

    return writes, commits, truncated
