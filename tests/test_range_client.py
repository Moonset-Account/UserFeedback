"""RangeClient 单元测试：区间请求、缓存合并、ETag 变化检测、200 拒绝。"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from selective_zip.errors import RangeNotSupported, SourceChanged
from selective_zip.range_client import RangeClient


class _RecordingHandler(BaseHTTPRequestHandler):
    payload = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 4  # 144 字节
    etag_value = '"v1"'
    log_requests: list[dict] = []

    def log_message(self, *args):
        return

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", self.etag_value)
        self.end_headers()

    def do_GET(self):
        range_header = self.headers.get("Range")
        self.log_requests.append({"range": range_header, "etag": self.etag_value})
        if not range_header:
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.payload)))
            self.end_headers()
            self.wfile.write(self.payload)
            return
        spec = range_header.removeprefix("bytes=")
        start_s, end_s = spec.split("-")
        start, end = int(start_s), int(end_s)
        chunk = self.payload[start : end + 1]
        self.send_response(206)
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(self.payload)}")
        self.send_header("ETag", self.etag_value)
        self.end_headers()
        self.wfile.write(chunk)


@pytest.fixture()
def server():
    _RecordingHandler.log_requests = []
    httpd = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    yield f"http://{host}:{port}", httpd, _RecordingHandler
    httpd.shutdown()


def test_head_metadata(server):
    url, _httpd, _handler = server
    client = RangeClient(url)
    meta = client.head()
    assert meta.total_size == len(_RecordingHandler.payload)
    assert meta.accept_ranges is True
    assert meta.etag == '"v1"'


def test_range_returns_exact_bytes_and_records(server):
    url, _httpd, _handler = server
    client = RangeClient(url)
    client.head()
    data = client.get_range(10, 19)
    assert data == _RecordingHandler.payload[10:20]
    record = client.records[-1]
    assert record.response_status == 206
    assert record.bytes_returned == 10
    assert record.content_range == f"bytes 10-19/{len(_RecordingHandler.payload)}"
    assert record.etag == '"v1"'


def test_overlapping_ranges_are_not_refetched(server):
    url, _httpd, handler = server
    client = RangeClient(url)
    client.head()
    client.get_range(0, 49)
    client.get_range(40, 99)  # 40-49 已缓存，只应请求 50-99
    client.get_range(20, 30)  # 完全在缓存内，不应发起新请求
    requests_after = len(handler.log_requests)
    assert client.get_range(20, 30) == handler.payload[20:31]
    assert len(handler.log_requests) == requests_after  # 无新增请求
    # 实际网络字节 = 50 + 50 = 100，而非 50 + 60 + 11
    assert client.total_downloaded_bytes == 100
    assert client.unique_cached_bytes == 100


def test_adjacent_ranges_merge(server):
    url, _httpd, handler = server
    client = RangeClient(url)
    client.head()
    client.get_range(0, 9)
    client.get_range(10, 19)
    assert client.unique_cached_bytes == 20
    assert client.total_downloaded_bytes == 20
    assert client.get_range(0, 19) == handler.payload[0:20]


def test_etag_change_aborts_with_source_changed(server):
    url, _httpd, handler = server
    client = RangeClient(url)
    client.head()
    client.get_range(0, 9)
    handler.etag_value = '"v2-changed"'  # 模拟源包在采集中被替换
    with pytest.raises(SourceChanged):
        client.get_range(20, 29)


class _NoRangeHandler(BaseHTTPRequestHandler):
    payload = b"X" * 32

    def log_message(self, *args):
        return

    def do_GET(self):
        # 忽略 Range，直接 200 返回整包
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)


def test_200_instead_of_206_is_rejected():
    httpd = HTTPServer(("127.0.0.1", 0), _NoRangeHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    try:
        client = RangeClient(f"http://{host}:{port}")
        with pytest.raises(RangeNotSupported):
            client.get_range(0, 7)
    finally:
        httpd.shutdown()
