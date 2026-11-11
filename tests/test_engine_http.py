"""采集引擎端到端测试（HTTP 协议子集，真实访问本地源站）。

真值 ground_truth 仅用于断言预期；采集器本身只走公开页面与下载接口。
"""
from __future__ import annotations

from pathlib import Path

import requests

from collector.engine import (
    CollectionEngine,
    DL_ERROR_HTML,
    DL_EXPIRED,
    DL_OK,
    OUT_FAILED_EXPIRED,
    OUT_FAILED_IDENTITY,
    OUT_SUCCESS,
    OUT_FAILED_VERSION,
    classify_download,
)
from collector.http_backend import HttpBackend
from collector.models import AcquireError, DownloadResponse
from generator.scenarios import BASE_VERSION, VERSION_STEP


def _run(engine, page_url, sid):
    return engine.run(sid, page_url(sid))


def test_normal_one_time_link_succeeds(http_backend, downloads_dir, page_url, truth):
    t = truth("normal")
    engine = CollectionEngine(http_backend, downloads_dir)
    result = _run(engine, page_url, "normal")

    assert result.status == OUT_SUCCESS == t["expect"]["outcome"]
    assert result.attempts == t["expect"]["attempts"] == 1
    assert result.refreshes == t["expect"]["refreshes"] == 0
    assert result.validation and result.validation["passed"] is True
    assert result.saved_path and result.run_id in result.saved_path
    assert Path(result.saved_path).read_bytes()[:4] == b"PK\x03\x04"

    # 一次性链接：再次使用必须失效
    link = next(e for e in result.events if e["kind"] == "link_acquired")["download_url"]
    again = requests.get(link, timeout=10)
    assert again.status_code == 410


def test_pre_expired_then_refresh_succeeds(http_backend, downloads_dir, page_url, truth):
    t = truth("pre_expired")
    engine = CollectionEngine(http_backend, downloads_dir)
    result = _run(engine, page_url, "pre_expired")

    assert result.status == OUT_SUCCESS
    assert result.attempts == t["expect"]["attempts"] == 2
    assert result.refreshes == t["expect"]["refreshes"] == 1
    kinds = [e["kind"] for e in result.events]
    assert "download_result" in kinds and "refreshing" in kinds and "version_confirmed" in kinds
    # 第一次结论必须明确识别为失效
    first_dl = next(e for e in result.events if e["kind"] == "download_result")
    assert first_dl["verdict"] == DL_EXPIRED


def test_error_html_with_status_200_is_classified_and_refreshed(
    http_backend, downloads_dir, page_url, truth
):
    t = truth("error_html")
    engine = CollectionEngine(http_backend, downloads_dir)
    result = _run(engine, page_url, "error_html")

    assert result.status == OUT_SUCCESS
    assert result.attempts == t["expect"]["attempts"] == 2
    first_dl = next(e for e in result.events if e["kind"] == "download_result")
    assert first_dl["verdict"] == DL_ERROR_HTML
    assert first_dl["status_code"] == 200
    assert "HTML 错误页" in first_dl["message"]


def test_already_used_link_refresh_succeeds(http_backend, downloads_dir, page_url):
    engine = CollectionEngine(http_backend, downloads_dir)
    result = _run(engine, page_url, "already_used")
    assert result.status == OUT_SUCCESS
    assert result.attempts == 2 and result.refreshes == 1
    first_dl = next(e for e in result.events if e["kind"] == "download_result")
    assert first_dl["verdict"] == DL_EXPIRED


def test_persistent_expired_hits_refresh_limit_no_infinite_loop(
    http_backend, downloads_dir, page_url, truth
):
    t = truth("persistent_expired")
    engine = CollectionEngine(http_backend, downloads_dir, max_refreshes=3)
    result = _run(engine, page_url, "persistent_expired")

    assert result.status == OUT_FAILED_EXPIRED
    assert result.refreshes == 3 == t["expect"]["refreshes"]
    assert result.attempts == 4 == t["expect"]["attempts"]
    assert "刷新上限" in result.message
    limit_event = next(e for e in result.events if e["kind"] == "refresh_limit")
    assert limit_event["max_refreshes"] == 3


def test_version_change_after_refresh_stops_and_explains(
    http_backend, downloads_dir, page_url, truth
):
    t = truth("version_change")
    engine = CollectionEngine(http_backend, downloads_dir)
    result = _run(engine, page_url, "version_change")

    assert result.status == OUT_FAILED_VERSION
    assert result.attempts == t["expect"]["attempts"] == 1  # 版本变化后不再下载
    assert result.refreshes == 1
    vc = next(e for e in result.events if e["kind"] == "version_changed")
    assert vc["baseline_version"] == BASE_VERSION
    assert vc["refreshed_version"] == VERSION_STEP
    # 刷新得到的新链接绝不能被下载（采集对象未被替换）
    assert sum(1 for e in result.events if e["kind"] == "download_attempt") == 1


def test_identity_mismatch_is_terminal_without_refresh(
    http_backend, downloads_dir, page_url, truth
):
    t = truth("identity_mismatch")
    engine = CollectionEngine(http_backend, downloads_dir)
    result = _run(engine, page_url, "identity_mismatch")

    assert result.status == OUT_FAILED_IDENTITY
    assert result.attempts == t["expect"]["attempts"] == 1
    assert result.refreshes == 0
    failed = next(c for c in result.validation["checks"] if not c["passed"])
    assert failed["name"] == "identity"
    assert "FOREIGN" in failed["detail"]


def test_js_page_cannot_be_acquired_by_http_backend(http_backend, page_url):
    try:
        http_backend.acquire_link(page_url("js_page"))
    except AcquireError as exc:
        assert "JavaScript" in str(exc) or "download-link" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("HTTP 后端不应能解析 JS 渲染页面的链接")


def test_classify_download_helpers():
    ok = DownloadResponse(200, {"Content-Type": "application/zip"}, b"PK\x03\x04xx", "u")
    assert classify_download(ok)[0] == DL_OK

    err200 = DownloadResponse(200, {"Content-Type": "text/html"}, b"<!doctype html><html>", "u")
    assert classify_download(err200)[0] == DL_ERROR_HTML

    gone = DownloadResponse(410, {"Content-Type": "text/html"}, b"<html>gone</html>", "u")
    assert classify_download(gone)[0] == DL_EXPIRED

    transport = DownloadResponse(0, {}, b"", "u", transport_error="timeout")
    verdict, _ = classify_download(transport)
    assert verdict == "transport"
