"""WARC 物理记录读取层。

职责（仅处理归档容器，不做字段提取）：

* 识别整体 gzip（多成员）归档与未压缩 WARC 文本归档；
* gzip 按成员切分：单个成员解压失败不影响后续成员，自动重新同步到下一个
  gzip 成员签名（1F 8B 08）；
* 每个成员 / 文本块按 WARC 头部 ``Content-Length`` 做长度分帧，
  response / request / revisit 均作为普通记录读出，并区分
  「WARC-Record-ID（归档记录标识）」与「WARC-Target-URI（目标 URL）」；
* HTTP 报文解帧复用成熟库 warcio（StatusAndHeadersParser /
  ArcWarcRecordLoader / ChunkedDataReader）。

支持的协议子集见 README「支持的协议子集」。
"""
from __future__ import annotations

import io
import zlib
from dataclasses import dataclass, field

from warcio.exceptions import ArchiveLoadFailed
from warcio.recordloader import (
    ArcWarcRecordLoader,
    ChunkedDataReader,
    StatusAndHeadersParser,
    StatusAndHeadersParserException,
)

GZIP_MAGIC = b"\x1f\x8b\x08"
_WARC_VERSIONS = ["WARC/1.1", "WARC/1.0"]


@dataclass
class RawRecord:
    """一条 WARC 记录的原始视图（提取前）。"""

    index: int
    rec_type: str
    record_id: str | None
    target_uri: str | None
    warc_date: str | None
    warc_headers: list[tuple[str, str]]
    http_statusline: str | None
    http_headers: list[tuple[str, str]]
    payload: bytes
    block: bytes
    file_offset: int
    member_index: int
    compressed: bool
    member_ok: bool
    errors: list[str] = field(default_factory=list)

    def header(self, name: str) -> str | None:
        name = name.lower()
        for k, v in self.warc_headers:
            if k.lower() == name:
                return v
        return None

    def http_header(self, name: str) -> str | None:
        name = name.lower()
        for k, v in self.http_headers:
            if k.lower() == name:
                return v
        return None

    def to_summary(self) -> dict:
        return {
            "index": self.index,
            "rec_type": self.rec_type,
            "record_id": self.record_id,
            "target_uri": self.target_uri,
            "warc_date": self.warc_date,
            "http_statusline": self.http_statusline,
            "file_offset": self.file_offset,
            "member_index": self.member_index,
            "compressed": self.compressed,
            "member_ok": self.member_ok,
            "errors": list(self.errors),
        }


@dataclass
class MemberError:
    member_index: int
    file_offset: int
    message: str
    partial_bytes: int


def _gzip_header_length(data: bytes, pos: int) -> int | None:
    """解析 gzip 成员头长度（RFC1952），无法解析返回 None。"""
    if data[pos:pos + 3] != GZIP_MAGIC:
        return None
    if pos + 10 > len(data):
        return None
    flg = data[pos + 3]
    p = pos + 10
    # FEXTRA
    if flg & 0x04:
        if p + 2 > len(data):
            return None
        xlen = int.from_bytes(data[p:p + 2], "little")
        p += 2 + xlen
    # FNAME / FCOMMENT：以 NUL 结尾
    for flag_bit in (0x08, 0x10):
        if flg & flag_bit:
            end = data.find(b"\x00", p)
            if end < 0:
                return None
            p = end + 1
    # FHCRC
    if flg & 0x02:
        p += 2
    return p - pos


def iter_gzip_members(data: bytes):
    """按成员切分 gzip 数据流。

    生成 ``(member_index, file_offset, decompressed_bytes, error_message)``。
    坏成员不会中断迭代：记录错误后扫描下一个成员签名重新同步。
    """
    pos = 0
    member_index = 0
    n = len(data)
    while pos < n:
        magic = data.find(GZIP_MAGIC, pos)
        if magic < 0:
            if pos < n:
                yield member_index, pos, b"", f"偏移 {pos} 起未找到下一个 gzip 成员，剩余 {n - pos} 字节被丢弃"
            break
        if magic > pos:
            yield member_index, pos, b"", (
                f"偏移 {pos}-{magic - 1} 的 {magic - pos} 字节不属于任何 gzip 成员，已跳过"
            )
            member_index += 1
            pos = magic
        hdr_len = _gzip_header_length(data, pos)
        if hdr_len is None:
            yield member_index, pos, b"", "gzip 成员头被截断或非法"
            member_index += 1
            pos += 3
            continue
        decompressor = zlib.decompressobj(31)
        try:
            out = decompressor.decompress(data[pos:])
            out += decompressor.flush()
        except zlib.error as exc:
            # 坏成员：不参与分帧，继续找下一个成员签名重新同步
            yield member_index, pos, b"", f"gzip 成员解压失败（{exc}），该成员内容不可读"
            member_index += 1
            next_pos = data.find(GZIP_MAGIC, pos + 3)
            if next_pos < 0:
                break
            pos = next_pos
            continue
        consumed = len(data) - pos - len(decompressor.unused_data)
        yield member_index, pos, out, None
        member_index += 1
        pos += max(consumed, hdr_len)


def _frame_plain_warc(block: bytes):
    """对一段（已解压的）WARC 字节按 Content-Length 分帧。

    生成 ``(record_bytes, frame_error)``；坏帧后尝试按 ``WARC/1.x`` 重新同步。
    """
    stream = io.BytesIO(block)
    parser = StatusAndHeadersParser(_WARC_VERSIONS)
    while True:
        start = stream.tell()
        if start >= len(block):
            return
        if not block[start:start + 5].startswith(b"WARC/"):
            nxt = block.find(b"\r\nWARC/1.", start)
            if nxt < 0:
                yield None, f"偏移 {start} 起无法识别的 WARC 帧，剩余 {len(block) - start} 字节被丢弃"
                return
            yield None, f"偏移 {start} 起存在 {nxt - start} 字节无法分帧的内容，已跳过"
            stream.seek(nxt + 2)
            continue
        try:
            headers = parser.parse(stream)
        except (StatusAndHeadersParserException, EOFError) as exc:
            yield None, f"WARC 记录头解析失败：{exc}"
            nxt = block.find(b"\r\nWARC/1.", start)
            if nxt < 0:
                return
            stream.seek(nxt + 2)
            continue
        length_raw = headers.get_header("Content-Length")
        try:
            length = int(length_raw)
        except (TypeError, ValueError):
            yield None, f"记录缺少合法 Content-Length（={length_raw!r}），无法按长度读取"
            nxt = block.find(b"\r\nWARC/1.", start)
            if nxt < 0:
                return
            stream.seek(nxt + 2)
            continue
        body = stream.read(length)
        if len(body) < length:
            yield None, f"记录体被截断：声明 {length} 字节，实际仅剩 {len(body)} 字节"
            return
        delimiter = stream.read(4)
        if delimiter != b"\r\n\r\n":
            # 不致命：warcio 对缺失分隔符较宽容，继续读下一帧，但给出诊断
            stream.seek(stream.tell() - len(delimiter))
        end = stream.tell()
        yield block[start:end], None


def _decode_http_body(record, declared_headers: list[tuple[str, str]]) -> tuple[bytes, str | None]:
    """从 warcio 记录中取出 HTTP 负载，处理 Content-Length / Transfer-Encoding。"""
    stream = record.content_stream()
    te = None
    for k, v in declared_headers:
        if k.lower() == "transfer-encoding":
            te = v.lower()
            break
    try:
        if te and "chunked" in te:
            reader = ChunkedDataReader(stream)
            body = reader.read()
        else:
            body = stream.read()
    except Exception as exc:  # 解帧失败属于坏记录，不上抛
        return b"", f"HTTP 报文解帧失败：{exc}"
    # 内容编码（仅支持 identity / gzip / deflate，其余原样返回并标注）
    ce = None
    for k, v in declared_headers:
        if k.lower() == "content-encoding":
            ce = v.lower().strip()
            break
    if ce in ("gzip", "x-gzip"):
        try:
            body = zlib.decompress(body, 31)
        except zlib.error as exc:
            return body, f"Content-Encoding=gzip 解压失败：{exc}"
    elif ce == "deflate":
        try:
            body = zlib.decompress(body)
        except zlib.error:
            try:
                body = zlib.decompress(body, -15)
            except zlib.error as exc:
                return body, f"Content-Encoding=deflate 解压失败：{exc}"
    elif ce and ce != "identity":
        return body, f"不支持的 Content-Encoding={ce}，负载为原始字节"
    return body, None


def _parse_record_bytes(record_bytes: bytes, index: int, member_index: int,
                        file_offset: int, compressed: bool) -> RawRecord:
    # verify_http=False：请求记录的状态行是 ``GET /path HTTP/1.1`` 而非
    # HTTP 版本行，严格模式会拒绝；本项目按 warcio 的 HTTP 动词解析器处理
    loader = ArcWarcRecordLoader(verify_http=False)
    errors: list[str] = []
    try:
        record = loader.parse_record_stream(
            io.BytesIO(record_bytes), known_format="warc"
        )
    except (ArchiveLoadFailed, StatusAndHeadersParserException, EOFError) as exc:
        # 头部级别失败：仍从原始字节里抢救基本标识
        head, _, _ = record_bytes.partition(b"\r\n\r\n")
        text = head.decode("latin1", "replace")
        rid = uri = date = None
        for line in text.split("\r\n"):
            if line.lower().startswith("warc-record-id:"):
                rid = line.split(":", 1)[1].strip().strip("<>")
            elif line.lower().startswith("warc-target-uri:"):
                uri = line.split(":", 1)[1].strip()
            elif line.lower().startswith("warc-date:"):
                date = line.split(":", 1)[1].strip()
        return RawRecord(
            index=index, rec_type="unknown", record_id=rid, target_uri=uri,
            warc_date=date, warc_headers=[], http_statusline=None,
            http_headers=[], payload=b"", block=record_bytes,
            file_offset=file_offset, member_index=member_index,
            compressed=compressed, member_ok=True,
            errors=[f"WARC 记录解析失败：{exc}"],
        )

    warc_headers = list(record.rec_headers.headers)
    http_headers = list(record.http_headers.headers) if record.http_headers else []
    statusline = record.http_headers.statusline if record.http_headers else None
    payload, http_error = _decode_http_body(record, http_headers)
    if http_error:
        errors.append(http_error)
    return RawRecord(
        index=index,
        rec_type=record.rec_type,
        record_id=(record.rec_headers.get_header("WARC-Record-ID") or "").strip("<>") or None,
        target_uri=record.rec_headers.get_header("WARC-Target-URI"),
        warc_date=record.rec_headers.get_header("WARC-Date"),
        warc_headers=warc_headers,
        http_statusline=statusline,
        http_headers=http_headers,
        payload=payload,
        block=record_bytes,
        file_offset=file_offset,
        member_index=member_index,
        compressed=compressed,
        member_ok=True,
        errors=errors,
    )


def read_warc_file(path: str) -> tuple[list[RawRecord], list[MemberError]]:
    """读取整个 WARC 文件，返回记录列表与成员级错误列表。

    设计为小中型归档工具：文件整体读入内存（上限由调用方约束）。
    """
    with open(path, "rb") as fh:
        data = fh.read()

    records: list[RawRecord] = []
    member_errors: list[MemberError] = []
    # 文件可能带非 gzip 前缀（脏数据），只要内含 gzip 成员签名就按成员流处理
    compressed = data[:3] == GZIP_MAGIC or GZIP_MAGIC in data

    if compressed:
        members = list(iter_gzip_members(data))
    else:
        members = [(0, 0, data, None)]

    index = 0
    for member_index, file_offset, block, member_error in members:
        if member_error is not None:
            member_errors.append(
                MemberError(member_index, file_offset, member_error, len(block))
            )
            continue
        for record_bytes, frame_error in _frame_plain_warc(block):
            if frame_error is not None:
                member_errors.append(
                    MemberError(member_index, file_offset, frame_error, 0)
                )
                continue
            raw = _parse_record_bytes(
                record_bytes, index, member_index, file_offset, compressed
            )
            records.append(raw)
            index += 1
    return records, member_errors
