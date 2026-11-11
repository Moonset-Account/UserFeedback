"""扫描器测试：跨页/页内重复去重、请求预算、扫描边界与协议子集。

期望数据来自 tests 独立真值（source_site.generator 的 golden），
由测试侧自行通过 HTTP 采集后核对，采集器本身不接触真值文件。
"""
from __future__ import annotations

import pytest
import requests

from collector.parsers import UnsupportedProtocolError, parse_json_page, parse_html_page
from collector.scanner import CatalogScanner, ScanError


def test_full_scan_matches_golden_unique_stream(demo_url, datasets):
    golden = datasets["demo"]["golden"]
    report = CatalogScanner(demo_url, request_budget=100).scan()

    assert report.stop.reason == "completed"
    observed_ids = [r.specimen_id for r in report.ordered_unique_records]
    # 采集到的唯一 ID 流与真值的首次出现顺序完全一致
    assert observed_ids == golden["unique_ids_in_order"]
    assert report.total_raw_entries == golden["raw_entry_count"]
    # 每页累计请求恰为页号（无重试发生时）
    assert [p.requests_used for p in report.pages] == list(range(1, len(report.pages) + 1))
    assert report.stop.next_url_not_fetched is None
    # 重复条目被逐页如实统计
    assert sum(p.duplicate_on_page for p in report.pages) == \
           golden["raw_entry_count"] - golden["unique_count"]


def test_intra_page_and_cross_page_duplicates_counted(demo_url, datasets):
    golden = datasets["demo"]["golden"]
    kinds = {d.get("kind", "cross_page") for d in golden["duplicate_refs"]}
    assert "intra_page" in kinds  # 夹具确实包含页内重复
    report = CatalogScanner(demo_url, request_budget=100).scan()
    pages_with_dup = [p for p in report.pages if p.duplicate_on_page > 0]
    assert pages_with_dup, "应至少有一页观察到重复条目"
    # 任何 ID 在唯一流中只出现一次——重复不增加入样机会的结构性前提
    ids = [r.specimen_id for r in report.ordered_unique_records]
    assert len(ids) == len(set(ids))


def test_budget_exhausted_stops_at_boundary(demo_url):
    # 预算 4：恰好抓 4 页，第 5 页 URL 被记录为未抓取边界
    report = CatalogScanner(demo_url, request_budget=4).scan()
    assert report.stop.reason == "budget_exhausted"
    assert len(report.pages) == 4
    assert report.stop.requests_used == 4
    assert report.stop.next_url_not_fetched and "cursor=5" in report.stop.next_url_not_fetched
    # 已观察集合仅来自前 4 页
    boundary_ids = {r.specimen_id for p in report.pages for r in p.records}
    assert set(r.specimen_id for r in report.ordered_unique_records) <= boundary_ids


def test_budget_smaller_than_one_page(demo_url):
    report = CatalogScanner(demo_url, request_budget=1).scan()
    assert report.stop.reason == "budget_exhausted"
    assert len(report.pages) == 1
    assert report.stop.next_url_not_fetched is not None


def test_retries_consume_budget_and_recover(monkeypatch, demo_url):
    # 让第 2 页首次请求返回 500、第二次成功：重试本身也计入预算
    real_get = requests.Session.get
    failed: set[str] = set()

    def flaky_get(self, url, *args, **kwargs):
        if "cursor=2" in url and url not in failed:
            failed.add(url)

            class _Bad:
                status_code = 500
                headers = {}
            return _Bad()
        return real_get(self, url, *args, **kwargs)

    monkeypatch.setattr(requests.Session, "get", flaky_get)
    report = CatalogScanner(demo_url, request_budget=100, max_retries=2, backoff_seconds=0).scan()
    assert report.stop.reason == "completed"
    # 总请求数 = 页数 + 1 次失败重试
    assert report.stop.requests_used == len(report.pages) + 1
    assert len(failed) == 1


def test_hard_http_error_fails_with_detail(demo_url, monkeypatch):
    def boom(self, url, *args, **kwargs):
        class _Resp:
            status_code = 404
            headers = {}
            def raise_for_status(self): pass
        return _Resp()
    monkeypatch.setattr(requests.Session, "get", boom)
    with pytest.raises(ScanError, match="404"):
        CatalogScanner(demo_url, request_budget=5, max_retries=0).scan()


def test_invalid_budget_rejected():
    with pytest.raises(ValueError):
        CatalogScanner("http://x", request_budget=0)


def test_json_parser_rejects_bad_payloads():
    with pytest.raises(UnsupportedProtocolError):
        parse_json_page(b"not-json", "http://x/api")
    with pytest.raises(UnsupportedProtocolError):
        parse_json_page(b'{"foo": 1}', "http://x/api")
    with pytest.raises(UnsupportedProtocolError):
        parse_json_page(b'{"items": [{"no_id": 1}]}', "http://x/api")


def test_html_parser_roundtrip_and_termination():
    html = b"""<html><body><div data-role="catalog">
      <article class="specimen" data-id="A"><a class="detail-link" href="/specimen/A">A</a></article>
      <article class="specimen" data-id="B"><a class="detail-link" href="/specimen/B">B</a></article>
    </div>
    <a data-role="next-page" href="/catalog?cursor=2">next</a></body></html>"""
    page = parse_html_page(html, "http://x/catalog")
    assert [r.specimen_id for r in page.records] == ["A", "B"]
    assert page.records[0].detail_url == "http://x/specimen/A"
    assert page.records[0].title == "A"  # 无 h3 时退回 ID
    assert page.next_url == "http://x/catalog?cursor=2"

    headed_html = html.replace(
        b'<article class="specimen" data-id="A"><a class="detail-link" href="/specimen/A">A</a>',
        b'<article class="specimen" data-id="A"><h3>Specimen A Title</h3><a class="detail-link" href="/specimen/A">link text</a>')
    headed = parse_html_page(headed_html, "http://x/catalog")
    assert headed.records[0].title == "Specimen A Title"  # 标题取 h3 而非链接文字

    last = parse_html_page(
        html.replace(b'<a data-role="next-page" href="/catalog?cursor=2">next</a>', b""),
        "http://x/catalog")
    assert last.next_url is None  # 无下一页元素即末页，不依赖 total


def test_unsupported_content_type():
    from collector.parsers import parse_page
    with pytest.raises(UnsupportedProtocolError):
        parse_page(b"<xml/>", "application/xml", "http://x/data")


def test_html_endpoint_scan(html_url):
    report = CatalogScanner(html_url, request_budget=100).scan()
    assert report.stop.reason == "completed"
    assert report.total_raw_entries > 0
    assert all(r.detail_url and r.detail_url.startswith("http") for r in report.ordered_unique_records)
