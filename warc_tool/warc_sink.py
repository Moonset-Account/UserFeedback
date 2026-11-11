"""WARC 写入辅助（样例生成器与采集器共用）。

封装 warcio 底层写法，支持：
* 每条记录独立 gzip 成员（WARC 多成员归档）或未压缩输出；
* 显式指定任意摘要标签（用于构造「坏摘要」边界样例，warcio 高层 API 会
  覆盖摘要，故这里直接构造 ArcWarcRecord 保留给定头部）；
* response / request / revisit / warcinfo 等记录类型。
"""
from __future__ import annotations

import io
import uuid

from warcio.recordloader import ArcWarcRecord
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from .digests import compute_digest


def new_record_id() -> str:
    return f"<urn:uuid:{uuid.uuid4()}>"


def _status_headers(statusline: str, headers: list[tuple[str, str]],
                    protocol: str) -> StatusAndHeaders:
    return StatusAndHeaders(statusline, list(headers), protocol=protocol)


class WarcSink:
    """顺序写入 WARC 记录的接收器。"""

    def __init__(self, path: str, gzip: bool = True):
        self.path = path
        self._fh = open(path, "wb")
        self.writer = WARCWriter(self._fh, gzip=gzip)
        self.gzip = gzip
        self.offsets: list[int] = []

    def _write(self, record: ArcWarcRecord) -> str:
        self.offsets.append(self._fh.tell())
        self.writer.write_record(record)
        return record.rec_headers.get_header("WARC-Record-ID").strip("<>")

    def write_warcinfo(self, fields: dict[str, str]) -> str:
        payload = "".join(f"{k}: {v}\r\n" for k, v in fields.items()).encode("utf-8")
        headers = StatusAndHeaders("", [
            ("WARC-Type", "warcinfo"),
            ("WARC-Record-ID", new_record_id()),
            ("WARC-Date", "2026-01-01T00:00:00Z"),
            ("Content-Type", "application/warc-fields"),
        ], protocol="WARC/1.1")
        record = ArcWarcRecord(
            "warc", "warcinfo", headers, io.BytesIO(payload), None,
            "application/warc-fields", len(payload),
        )
        return self._write(record)

    def write_response(self, uri: str, date: str, status: int, reason: str,
                       http_headers: list[tuple[str, str]], body: bytes,
                       record_id: str | None = None,
                       payload_digest: str | None = None,
                       block_digest: str | None = None,
                       extra_warc_headers: dict[str, str] | None = None) -> str:
        """写 response 记录。payload_digest 可显式给定（含故意写错的摘要）。"""
        rid = record_id or new_record_id()
        label = payload_digest or compute_digest(body)
        warc_headers = [
            ("WARC-Type", "response"),
            ("WARC-Record-ID", rid),
            ("WARC-Target-URI", uri),
            ("WARC-Date", date),
            ("WARC-Payload-Digest", label),
            ("Content-Type", "application/http; msgtype=response"),
        ]
        if block_digest:
            warc_headers.append(("WARC-Block-Digest", block_digest))
        for k, v in (extra_warc_headers or {}).items():
            warc_headers.append((k, v))
        wh = StatusAndHeaders("", warc_headers, protocol="WARC/1.1")
        hh = _status_headers(f"{status} {reason}", http_headers, "HTTP/1.1")
        record = ArcWarcRecord(
            "warc", "response", wh, io.BytesIO(body), hh,
            "application/http; msgtype=response", len(body),
        )
        record.payload_length = len(body)
        return self._write(record)

    def write_request(self, uri: str, date: str, method: str,
                      http_headers: list[tuple[str, str]], body: bytes = b"",
                      concurrent_to: str | None = None,
                      record_id: str | None = None) -> str:
        rid = record_id or new_record_id()
        warc_headers = [
            ("WARC-Type", "request"),
            ("WARC-Record-ID", rid),
            ("WARC-Target-URI", uri),
            ("WARC-Date", date),
            ("Content-Type", "application/http; msgtype=request"),
        ]
        if concurrent_to:
            warc_headers.append(("WARC-Concurrent-To", concurrent_to))
        wh = StatusAndHeaders("", warc_headers, protocol="WARC/1.1")
        hh = _status_headers(method, http_headers, "HTTP/1.1")
        record = ArcWarcRecord(
            "warc", "request", wh, io.BytesIO(body), hh,
            "application/http; msgtype=request", len(body),
        )
        record.payload_length = len(body)
        return self._write(record)

    def write_revisit(self, uri: str, date: str,
                      http_headers: list[tuple[str, str]],
                      payload_digest: str,
                      refers_to: str | None = None,
                      refers_to_uri: str | None = None,
                      refers_to_date: str | None = None,
                      profile_1_0: bool = False,
                      record_id: str | None = None) -> str:
        """写 revisit 记录。引用三元组至少给一组，也可故意给不存在的引用。"""
        rid = record_id or new_record_id()
        profile = (
            "http://netpreserve.org/warc/1.0/revisit/identical-payload-digest"
            if profile_1_0
            else "http://netpreserve.org/warc/1.1/revisit/server-not-modified"
        )
        warc_headers = [
            ("WARC-Type", "revisit"),
            ("WARC-Record-ID", rid),
            ("WARC-Target-URI", uri),
            ("WARC-Date", date),
            ("WARC-Profile", profile),
            ("WARC-Payload-Digest", payload_digest),
            ("Content-Type", "application/http; msgtype=response"),
        ]
        if refers_to:
            warc_headers.append(("WARC-Refers-To", refers_to))
        if refers_to_uri:
            warc_headers.append(("WARC-Refers-To-Target-URI", refers_to_uri))
        if refers_to_date:
            warc_headers.append(("WARC-Refers-To-Date", refers_to_date))
        wh = StatusAndHeaders("", warc_headers, protocol="WARC/1.1")
        hh = _status_headers("200 OK", http_headers, "HTTP/1.1")
        # revisit 记录块只有响应头、没有正文
        block_stream = io.BytesIO(b"")
        record = ArcWarcRecord(
            "warc", "revisit", wh, block_stream, hh,
            "application/http; msgtype=response", 0,
        )
        record.payload_length = 0
        return self._write(record)

    def close(self):
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
