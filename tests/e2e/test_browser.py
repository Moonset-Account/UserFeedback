"""浏览器端到端验证（Playwright）。

未安装 playwright 或 chromium 时自动 skip：
    pip install playwright && python -m playwright install chromium
运行：python -m pytest tests/e2e -q
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

playwright = pytest.importorskip("playwright", reason="未安装 playwright，浏览器 E2E 跳过")
from playwright.sync_api import expect, sync_playwright  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "app.py"), "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    # 等待端口就绪（最多 20 秒，覆盖首次 venv 引导）
    deadline = time.time() + 20
    import urllib.request

    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{base_url}/api/scenarios", timeout=1)
            break
        except Exception:
            time.sleep(0.4)
    else:
        out = proc.stdout.read() if proc.stdout else ""
        pytest.fail(f"采集台未在规定时间内启动：\n{out}")
    yield base_url
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_demo_button_runs_normal_and_boundary_within_budget(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(live_server + "/", wait_until="networkidle")

        # 首屏关键元素可见
        expect(page.locator("#run-demo")).to_be_visible()
        expect(page.locator("#run-all")).to_be_visible()
        expect(page.locator("#scenario-select")).to_be_visible()

        started = time.perf_counter()
        page.click("#run-demo")
        # 两张结果卡片出现：一个 complete、一个 rejected
        page.wait_for_selector(".card.badge-complete, .card .badge.complete", timeout=15000)
        page.wait_for_selector(".card .badge.rejected", timeout=15000)
        elapsed = time.perf_counter() - started
        assert elapsed < 15, f"演示场景耗时 {elapsed:.1f}s 超过 15 秒预算"

        cards = page.locator(".card")
        assert cards.count() == 2
        # 结果保留在页面可展开核对
        page.locator(".card .card-head").first.click()
        expect(page.locator(".card.open .evidence").first).to_be_visible()

        # 再跑完整验收：6 个场景、四种结论齐全
        page.click("#run-all")
        page.wait_for_function(
            "document.querySelectorAll('.card').length === 6", timeout=20000)
        for cls in [".badge.complete", ".badge.fault", ".badge.rejected"]:
            assert page.locator(cls).count() >= 1
        browser.close()
