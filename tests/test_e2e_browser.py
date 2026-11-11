"""端到端（真实浏览器）：Playwright/Chromium 两条链路。

1. 采集器浏览器通道（BrowserCatalogScanner）经真实 DOM 采集 HTML 目录；
2. 浏览器打开中文网页，点击 id=run-demo 的“运行示例”按钮，验证两个场景在 15 秒内出结果。

若环境未安装 Playwright/Chromium，整个模块跳过（不伪装通过）。
"""
from __future__ import annotations

import time

import pytest

from collector.browser import BrowserCatalogScanner, BrowserUnavailable
from collector.runner import run_sampling_task

playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402


def _chromium_available(pw) -> bool:
    try:
        browser = pw.chromium.launch(headless=True)
        browser.close()
        return True
    except Exception:
        return False


@pytest.fixture()
def pw_ctx():
    """每个测试独立的 Playwright 连接：sync API 与创建它的线程绑定，不可跨用例复用。"""
    with sync_playwright() as pw:
        if not _chromium_available(pw):
            pytest.skip("Chromium 内核不可用，跳过浏览器端到端测试")
        yield pw


@pytest.fixture()
def chromium(pw_ctx):
    browser = pw_ctx.chromium.launch(headless=True)
    yield browser
    browser.close()


def test_browser_scanner_html_catalog(chromium, html_url, datasets):
    golden = datasets["demo"]["golden"]
    scanner = BrowserCatalogScanner(
        html_url, request_budget=30, headless=True, external_browser=chromium)
    result = run_sampling_task(
        "browser-1", html_url, seed=99, sample_size=8, request_budget=30, scanner=scanner)
    assert result.status == "ok"
    assert result.params["transport"] == "browser"
    assert result.observed_unique_count == golden["unique_count"]
    assert len(result.sampling["sample"]) == 8
    # 来源证据为 HTML 页面
    assert all("catalog" in p["url"] for p in result.scan["pages"])


def test_browser_scanner_respects_budget(chromium, html_url):
    scanner = BrowserCatalogScanner(
        html_url, request_budget=3, headless=True, external_browser=chromium)
    result = run_sampling_task(
        "browser-budget", html_url, seed=5, sample_size=6, request_budget=3, scanner=scanner)
    assert result.stop_reason == "budget_exhausted"
    assert result.requests_used == 3
    assert "全站无偏样本" in result.declaration


def test_browser_scanner_owns_browser_when_not_injected(html_url, datasets):
    # 未注入浏览器时自行管理生命周期（独立脚本使用路径）
    golden = datasets["demo"]["golden"]
    scanner = BrowserCatalogScanner(html_url, request_budget=30, headless=True)
    result = run_sampling_task(
        "browser-own", html_url, seed=7, sample_size=5, request_budget=30, scanner=scanner)
    assert result.status == "ok"
    assert result.observed_unique_count == golden["unique_count"]


def test_browser_unavailable_message(monkeypatch):
    import collector.browser as bmod

    def fail_import():
        raise BrowserUnavailable("x")
    monkeypatch.setattr(bmod, "_import_playwright", lambda: (_ for _ in ()).throw(
        BrowserUnavailable("未安装 Playwright。请执行：pip install playwright")))
    with pytest.raises(BrowserUnavailable):
        bmod.BrowserCatalogScanner("http://x", 5).scan()


def test_ui_demo_button_in_real_browser(pw_ctx, source_server):
    # 启动 Web 应用（真实线程 + 真实源站）
    import threading
    from werkzeug.serving import make_server
    from webapp.app import create_app

    import socket
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    app = create_app(source_server)
    httpd = make_server("127.0.0.1", port, app, threaded=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        browser = pw_ctx.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
        assert page.title().startswith("公开标本目录")

        demo_btn = page.locator("#run-demo")
        assert demo_btn.is_visible()
        t0 = time.monotonic()
        demo_btn.click()

        page.wait_for_function(
            "() => document.querySelectorAll('.task-item').length >= 2 && "
            "[...document.querySelectorAll('.task-item .badge')]"
            ".every(b => /已完成|执行出错/.test(b.textContent))",
            timeout=15000)
        elapsed = time.monotonic() - t0
        assert elapsed < 15

        # 两个场景结果保留且可点击查看；预算边界场景必须展示受限声明
        titles = page.locator(".task-item .t-title span:first-child").all_inner_texts()
        assert any("正常" in t for t in titles) and any("边界" in t for t in titles)
        page.locator(".task-item", has_text="边界").first.click()
        page.wait_for_selector("#declaration.limited")
        decl = page.locator("#declaration").inner_text()
        assert "已观察" in decl and "全站" in decl
        # 样本来源表有真实链接
        assert page.locator("#sample-table tbody tr").count() > 0
        # 频率检验可运行（先展开折叠面板再点击）
        page.locator(".freq-block").evaluate("el => { el.open = true; }")
        page.locator("#run-freq").click()
        page.wait_for_selector(".freq-pass, .freq-fail", timeout=20000)
        assert page.locator(".freq-disclaimer").inner_text().find("不构成全站覆盖证明") >= 0
        browser.close()
    finally:
        httpd.shutdown()
