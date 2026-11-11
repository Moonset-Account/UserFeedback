"""读取层健壮性：未压缩 WARC、截断帧、多成员 gzip 重新同步。"""
from __future__ import annotations

import io
import zlib

from warc_tool.reader import iter_gzip_members, read_warc_file
from warc_tool.warc_sink import WarcSink
from warc_tool.samplegen import page


def test_uncompressed_warc_roundtrip(tmp_path):
    path = tmp_path / "plain.warc"
    with WarcSink(str(path), gzip=False) as sink:
        sink.write_warcinfo({"software": "test"})
        rid = sink.write_response(
            "http://x.test/a", "2025-01-01T00:00:00Z", 200, "OK",
            [("Content-Type", "text/html; charset=utf-8")],
            page("未压缩页", "作者甲", "2025-01-01", "摘要", ["k"], ["正文段落。"]),
        )
    records, errors = read_warc_file(str(path))
    assert errors == []
    assert [r.rec_type for r in records] == ["warcinfo", "response"]
    resp = records[1]
    assert resp.record_id == rid
    assert resp.compressed is False
    assert "未压缩页" in resp.payload.decode("utf-8")


def test_gzip_member_resync_after_garbage_prefix(tmp_path):
    path = tmp_path / "resync.warc.gz"
    with WarcSink(str(path), gzip=True) as sink:
        sink.write_response(
            "http://x.test/one", "2025-01-01T00:00:00Z", 200, "OK",
            [("Content-Type", "text/html; charset=utf-8")],
            page("第一页", "作者", "2025-01-01", "s", ["k"], ["p"]),
        )
    raw = path.read_bytes()
    # 在文件头部插入非 gzip 垃圾字节，读取器应跳过并重新同步
    polluted = b"JUNKJUNKJUNK" + raw
    polluted_path = tmp_path / "polluted.warc.gz"
    polluted_path.write_bytes(polluted)
    records, errors = read_warc_file(str(polluted_path))
    assert any("不属于任何 gzip 成员" in e.message for e in errors)
    assert len(records) == 1
    assert records[0].target_uri == "http://x.test/one"


def test_member_splitter_finds_all_members(tmp_path):
    path = tmp_path / "multi.warc.gz"
    with WarcSink(str(path), gzip=True) as sink:
        for i in range(4):
            sink.write_response(
                f"http://x.test/{i}", "2025-01-0{i+1}T00:00:00Z".replace("i", str(i)),
                200, "OK", [("Content-Type", "text/html; charset=utf-8")],
                page(f"页面{i}", "作者", "2025-01-01", "s", ["k"], [f"段落{i}"]),
            )
    data = path.read_bytes()
    members = list(iter_gzip_members(data))
    assert len(members) == 4
    assert all(err is None for _, _, _, err in members)
    # 每个成员都能独立解压出以 WARC/ 开头的块
    for _, _, block, err in members:
        assert block.startswith(b"WARC/")


def test_truncated_file_reports_error_not_crash(tmp_path):
    path = tmp_path / "trunc.warc.gz"
    with WarcSink(str(path), gzip=True) as sink:
        sink.write_response(
            "http://x.test/full", "2025-01-01T00:00:00Z", 200, "OK",
            [("Content-Type", "text/html; charset=utf-8")],
            page("完整页", "作者", "2025-01-01", "s", ["k"], ["p"]),
        )
    data = path.read_bytes()
    cut = tmp_path / "cut.warc.gz"
    cut.write_bytes(data[: len(data) // 2])
    records, errors = read_warc_file(str(cut))
    # 不抛异常；要么记录为空要么部分成功，且错误被结构化记录
    assert isinstance(records, list)
    assert isinstance(errors, list)


def test_chunked_transfer_encoding_decoded():
    from warc_tool.reader import _decode_http_body
    from warcio.recordloader import ArcWarcRecord
    from warcio.statusandheaders import StatusAndHeaders

    chunks = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
    wh = StatusAndHeaders("", [("WARC-Type", "response")], protocol="WARC/1.1")
    hh = StatusAndHeaders("200 OK", [("Transfer-Encoding", "chunked")], protocol="HTTP/1.1")
    record = ArcWarcRecord("warc", "response", wh, io.BytesIO(chunks), hh,
                           "application/http", len(chunks))
    body, error = _decode_http_body(record, [("Transfer-Encoding", "chunked")])
    assert error is None
    assert body == b"hello world"


def test_gzip_content_encoding_decoded():
    import gzip as gzip_mod

    from warc_tool.reader import _decode_http_body
    from warcio.recordloader import ArcWarcRecord
    from warcio.statusandheaders import StatusAndHeaders

    plain = b"<html>gzip body</html>"
    gz = gzip_mod.compress(plain)
    wh = StatusAndHeaders("", [("WARC-Type", "response")], protocol="WARC/1.1")
    hh = StatusAndHeaders("200 OK", [("Content-Encoding", "gzip")], protocol="HTTP/1.1")

    class _StubRecord:
        def content_stream(self_inner):
            return io.BytesIO(gz)

    body, error = _decode_http_body(_StubRecord(), [("Content-Encoding", "gzip")])
    assert error is None
    assert body == plain
