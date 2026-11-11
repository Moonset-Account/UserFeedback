"""Playwright 端到端验证：真实浏览器操作中文页面。

运行：.venv/bin/python -m playwright install chromium  （仅首次需要）
      .venv/bin/python -m pytest tests/test_e2e_browser.py -q
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "app.py"),
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**__import__("os").environ, "XINGLAN_VENV_BOOTSTRAPPED": "1"},
    )
    base = f"http://127.0.0.1:{port}"
    import urllib.request
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1).read()
            break
        except Exception:
            time.sleep(0.3)
    else:
        proc.kill()
        pytest.fail("Web 应用启动超时")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _wait_jobs_done(page, timeout_ms=15000):
    page.wait_for_function(
        """() => {
            const pills = document.querySelectorAll('.job-row .pill');
            if (pills.length < 2) return false;
            return [...pills].every(p => /完成|失败/.test(p.textContent));
        }""",
        timeout=timeout_ms,
    )


def test_run_demo_button_and_cards_in_browser(live_server):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(live_server + "/", wait_until="networkidle")

        # 关键元素首屏可见
        assert page.is_visible("#run-demo")
        demo_box = page.locator("#run-demo").bounding_box()
        assert demo_box["y"] < 720 and demo_box["x"] + demo_box["width"] <= 1280
        assert page.is_visible("#upload-form")
        assert page.is_visible("#job-list")

        # 点击「运行示例」
        page.click("#run-demo")
        _wait_jobs_done(page)

        # 两个任务都保留且完成
        rows = page.locator(".job-row")
        assert rows.count() == 2
        assert page.locator(".job-row .pill-done").count() == 2

        # 按标签精确选中「正常场景」，检查 revisit 卡片与字段（不依赖行顺序）
        page.locator(".job-row", has_text="正常场景").first.click()
        page.wait_for_selector(".card", timeout=10000)
        cards = page.locator(".card")
        assert cards.count() >= 3
        # revisit 卡片带复用标记
        revisit_card = page.locator(".card.revisit")
        assert revisit_card.count() == 1
        assert "warc-refers-to-record-id" in revisit_card.inner_text()
        # 字段卡片含标题与证据
        assert "观测简报第 001 期" in page.locator("#cards").inner_text()
        assert "证据：" in page.locator("#cards").inner_text()

        # 打开原始响应抽屉
        page.locator("button[data-raw]").first.click()
        page.wait_for_selector("#raw-drawer.open", timeout=5000)
        page.wait_for_timeout(400)  # 等待滑入过渡结束
        drawer_text = page.locator("#drawer-body").inner_text()
        assert "WARC-Record-ID" in drawer_text
        assert "WARC-Target-URI" in drawer_text
        assert "HTTP 响应行" in drawer_text
        # 抽屉右缘不超出视口
        drawer_box = page.locator("#raw-drawer").bounding_box()
        assert drawer_box["x"] + drawer_box["width"] <= 1281
        page.click("#drawer-close")
        page.wait_for_selector("#raw-drawer:not(.open)", timeout=5000)
        assert not page.locator("#raw-drawer").evaluate("el => el.classList.contains('open')")

        # 切换到「边界场景」，错误与坏摘要可见
        page.locator(".job-row", has_text="边界场景").first.click()
        page.wait_for_selector(".card.has-error", timeout=10000)
        boundary_text = page.locator("#cards").inner_text()
        assert "坏摘要" in boundary_text
        assert "不存在" in boundary_text  # 缺失引用
        assert "gzip 成员解压失败" in page.locator("#cards").inner_text()
        # 坏成员之后的正常记录仍在
        assert "星澜海洋研究院 · 首页" in boundary_text

        browser.close()


def test_acceptance_entry_in_browser(live_server):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(live_server + "/", wait_until="networkidle")
        page.click("#run-acceptance")
        page.wait_for_selector(".job-row .pill-done", timeout=15000)
        page.locator(".job-row", has_text="完整验收场景").first.click()
        page.wait_for_selector(".card", timeout=10000)
        text = page.locator("#cards").inner_text()
        # 两种引用规则都出现
        assert "warc-refers-to-uri-and-date" in text
        assert "warc-refers-to-record-id" in text
        browser.close()
