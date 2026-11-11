"""浏览器端到端（Playwright）：真实打开页面，点击 #run-demo，验证 15 秒目标。

运行前提：pip install -r requirements-dev.txt && python -m playwright install chromium
未安装 Playwright 或浏览器时本模块自动 skip，不影响其余测试。
"""
import socket
import threading
from contextlib import closing

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

from mailacq.webapp import create_app


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    app = create_app()
    thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port,
                               use_reloader=False), daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            launched = p.chromium.launch()
        except Exception as exc:  # 浏览器二进制未安装
            pytest.skip(f"Chromium 不可用，跳过浏览器 E2E: {exc}")
        yield launched
        launched.close()


def test_run_demo_button_in_browser(server, browser):
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    page.goto(server + "/", wait_until="networkidle")

    # 首屏关键元素可见
    assert page.is_visible("#run-demo")
    assert page.is_visible("#scenario-select")
    assert page.is_visible("#runs-table")

    page.click("#run-demo")
    page.wait_for_selector("#run-status.ok", timeout=15000)
    status_text = page.text_content("#run-status")
    assert "示例完成" in status_text

    # 总览表出现两条运行记录
    rows = page.query_selector_all("#runs-table tbody tr[data-run]")
    assert len(rows) >= 2

    # 点击第一行，详情面板出现证据锚点
    rows[0].click()
    page.wait_for_selector("#detail-body .msg-card", timeout=5000)
    assert page.is_visible("#detail-body")
    assert "part" in page.text_content("#detail-body")

    # 1280×720 下首屏无横向滚动条
    scroll_width = page.evaluate("document.documentElement.scrollWidth")
    assert scroll_width <= 1280
    page.close()
