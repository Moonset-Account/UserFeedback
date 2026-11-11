"""Playwright 浏览器端到端验证。

验证真实浏览器中：
- 根路径页面加载、协议子集展示；
- id 为 run-demo 的"运行示例"按钮依次跑正常 + 边界两个场景；
- 两个任务结果都保留可查看，边界场景显示明确错误；
- 页面适配 1280×720，关键操作首屏可见。

未安装 Playwright 或浏览器时自动 skip（HTTP 层 E2E 已由其余测试覆盖）。
"""

from __future__ import annotations

import threading
import time
import urllib.request

import pytest

from source_site.server import start_source_server
from web.app import create_web_server

playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402


@pytest.fixture()
def live_web(sample_data_dir):
    source_server, _st, source_base = start_source_server(str(sample_data_dir), "127.0.0.1", 0)
    web_server = create_web_server("127.0.0.1", 0, source_base, str(sample_data_dir))
    thread = threading.Thread(target=web_server.serve_forever, daemon=True)
    thread.start()
    host, port = web_server.server_address
    base = f"http://{host}:{port}"
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/meta", timeout=1) as resp:
                if resp.status == 200:
                    break
        except OSError:
            time.sleep(0.1)
    yield base
    web_server.shutdown()
    source_server.shutdown()


@pytest.fixture()
def browser_page():
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # 浏览器未安装
            pytest.skip(f"Chromium 不可用：{exc}")
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        yield page
        browser.close()


def test_demo_button_runs_two_scenarios_in_browser(live_web, browser_page):
    page = browser_page
    page.goto(live_web + "/", wait_until="networkidle")

    # 首屏关键元素可见
    assert page.is_visible("#run-demo")
    assert page.is_visible("#run-task")
    assert page.is_visible("#scenario-list")
    subset_text = page.text_content("#protocol-subset")
    assert "ZIP64" in subset_text

    # 点击"运行示例"，等待两个任务全部结束（要求 15 秒内）
    page.click("#run-demo")
    deadline = time.time() + 15
    succeeded = False
    while time.time() < deadline:
        status_text = page.text_content("#demo-status") or ""
        if "两个示例均已结束" in status_text:
            succeeded = True
            break
        time.sleep(0.3)
    assert succeeded, "运行示例未在 15 秒内完成"

    # 至少保留两个任务标签（正常 + 边界）
    tabs = page.query_selector_all(".task-tab")
    assert len(tabs) >= 2

    # 找到边界(CRC错误)任务标签并点开（轮询会重绘 DOM，点击前重新查询句柄）
    edge_tab = page.locator(".task-tab", has_text="边界").first
    edge_tab.wait_for(state="visible", timeout=5000)
    edge_tab.click()
    page.wait_for_selector(".error-box", timeout=5000)
    error_text = page.text_content(".error-box")
    assert "crc_mismatch" in error_text

    # 正常任务标签点开后应有正式记录与 Range 证据表
    normal_tab = page.locator(".task-tab", has_text="正常").first
    normal_tab.wait_for(state="visible", timeout=5000)
    normal_tab.click()
    page.wait_for_selector(".summary.success", timeout=5000)
    summary = page.text_content(".summary")
    assert "正式记录：2 条成员" in summary
    # 字节区间表存在且行数 > 0
    range_rows = page.query_selector_all(".detail-block table tbody tr")
    assert len(range_rows) > 0


def test_manual_scenario_selection_and_result(live_web, browser_page):
    page = browser_page
    page.goto(live_web + "/", wait_until="networkidle")
    # 通过场景清单里 encrypted 项的按钮选择加密场景
    items = page.query_selector_all(".scenario-item")
    target = None
    for item in items:
        if "encrypted" in item.text_content():
            target = item
            break
    assert target is not None
    target.query_selector("button").click()
    page.click("#run-task")
    page.wait_for_selector(".summary.failed", timeout=10000)
    page.wait_for_selector(".error-box", timeout=5000)
    assert "encrypted_archive" in page.text_content(".error-box")
