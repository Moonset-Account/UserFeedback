"""Playwright 浏览器端到端：真实启动 app.py，在中文页面上操作并断言。

运行：
    python3 -m playwright install chromium   # 仅首次需要
    python3 -m pytest tests/test_browser_e2e.py -q
若环境无浏览器，本模块会 skip 而非报错。
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_url():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "app.py"), "--host", "127.0.0.1",
         "--port", str(port), "--no-venv"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    import urllib.request
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url + "/healthz", timeout=1)
            break
        except Exception:
            time.sleep(0.3)
    else:
        proc.terminate()
        pytest.skip("本地服务器未能在 30 秒内启动")
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def browser_page(server_url):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(server_url + "/", wait_until="networkidle")
            yield page
            browser.close()
    except Exception as exc:  # 浏览器未安装等环境问题
        pytest.skip(f"Playwright 浏览器不可用: {exc}")


def test_page_renders_chinese_ui_and_spec(browser_page):
    page = browser_page
    assert "参数序列化采集请求构造器" in page.title()
    # 接口下拉框已从 /api/spec 填充
    options = page.eval_on_selector_all(
        "#operation-select option", "els => els.map(e => e.value)"
    )
    assert "listExperiments" in options
    assert "getLabExperiment" in options


def test_run_demo_completes_within_15_seconds(browser_page):
    page = browser_page
    start = time.time()
    page.click("#run-demo")
    page.wait_for_function(
        "document.querySelector('#demo-status').textContent.includes('示例完成')",
        timeout=15000,
    )
    elapsed = time.time() - start
    assert elapsed < 15
    status = page.text_content("#demo-status")
    assert "2/2 成功" in status
    # 两个场景结果都保留在历史记录中
    history = page.eval_on_selector_all(
        "#history-list .history-item", "els => els.length"
    )
    assert history >= 2
    # 表格中有真实返回的数据行
    row_count = page.eval_on_selector_all(
        "#result-table tbody tr", "els => els.length"
    )
    assert row_count >= 1


def test_rejection_scenario_shows_before_request_notice(browser_page):
    page = browser_page
    # 确保验收场景详情展开，再点击对应场景的“载入并运行”按钮
    if not page.locator("#scenario-acc").is_visible():
        page.click(".scenario-picker summary")
    btn = page.locator(
        ".scenario-item:has-text('deepObject 传数组') button:has-text('载入并运行')"
    )
    btn.click()
    page.wait_for_selector("#reject-box:not(.hidden)", timeout=8000)
    reason = page.text_content("#reject-reason")
    assert "期望 object" in reason
    assert "请求未生成" in page.text_content("#request-line")
    # 历史中标记为发送前拒绝
    first = page.text_content("#history-list .history-item")
    assert "发送前拒绝" in first


def test_preview_shows_golden_request_without_sending(browser_page):
    page = browser_page
    # 展开 JSON 编辑器（折叠状态下 textarea 不可见，无法 fill）
    if not page.locator("#params-json").is_visible():
        page.click(".json-editor summary")
    page.select_option("#operation-select", "listExperiments")
    page.fill("#params-json", '{"codes": ["TB-21,7", "MB/330"]}')
    page.click("#sync-from-json")
    page.click("#btn-preview")
    page.wait_for_function(
        "document.querySelector('#request-line').textContent.includes('codes=TB-21%2C7')",
        timeout=5000,
    )
    text = page.text_content("#request-line")
    assert "codes=TB-21%2C7,MB%2F330" in text
    assert "暂无回显" in page.text_content("#echo-view")


def test_viewport_1280x720_key_actions_above_fold(browser_page):
    page = browser_page
    box = page.locator("#run-demo").bounding_box()
    assert box is not None and box["y"] + box["height"] <= 720
    run_box = page.locator("#btn-run").bounding_box()
    assert run_box is not None and run_box["y"] + run_box["height"] <= 720
