"""Playwright 浏览器协议子集测试。

浏览器不可用时整体跳过（README 已说明安装命令）。
"""
from __future__ import annotations

import pytest

from collector.engine import CollectionEngine, OUT_SUCCESS
from collector.playwright_backend import PlaywrightBackend

pytestmark = pytest.mark.playwright


@pytest.fixture
def playwright_backend():
    backend = PlaywrightBackend(headless=True)
    try:
        yield backend
    finally:
        backend.close()


def test_js_rendered_page_collected_via_browser(
    playwright_backend, downloads_dir, page_url, truth
):
    t = truth("js_page")
    assert t["js_rendered"] is True

    link = playwright_backend.acquire_link(page_url("js_page"))
    assert link.download_url.startswith("http")
    assert link.experiment_id == t["experiment_id"]
    assert link.version == t["base_version"]
    assert link.evidence["rendered_by_javascript"] is True

    engine = CollectionEngine(playwright_backend, downloads_dir)
    result = engine.run("js_page", page_url("js_page"))
    assert result.status == OUT_SUCCESS
    assert result.attempts == 1
    assert result.validation and result.validation["passed"] is True


def test_browser_backend_also_handles_server_rendered_page(
    playwright_backend, downloads_dir, page_url
):
    engine = CollectionEngine(playwright_backend, downloads_dir)
    result = engine.run("normal", page_url("normal"))
    assert result.status == OUT_SUCCESS
    assert result.attempts == 1


def test_browser_backend_recovers_from_pre_expired(
    playwright_backend, downloads_dir, page_url
):
    engine = CollectionEngine(playwright_backend, downloads_dir)
    result = engine.run("pre_expired", page_url("pre_expired"))
    assert result.status == OUT_SUCCESS
    assert result.refreshes == 1 and result.attempts == 2
