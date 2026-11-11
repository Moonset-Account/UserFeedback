"""端到端验证：启动真实 Flask 服务，用 Chromium 操作中文网页。

运行：python -m playwright install chromium  （仅首次需要）
     python -m pytest tests/test_e2e.py -q
若浏览器内核缺失，本模块自动 skip，不影响单元测试。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import closing

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "app.py"),
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 40
    while time.time() < deadline:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.4)
    else:
        out = proc.stdout.read() if proc.stdout else ""
        pytest.fail(f"服务启动超时：\n{out}")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def browser():
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            yield browser
            browser.close()
    except Exception as exc:  # 内核未安装等环境问题
        pytest.skip(f"Playwright Chromium 不可用，跳过端到端：{exc}")


def _page(browser, server):
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    page.goto(server, wait_until="networkidle")
    return page


def test_page_loads_chinese_ui_and_demo_within_15s(browser, server):
    page = _page(browser, server)
    # 首屏关键元素
    assert "SVG 实验图数值采集器" in page.title()
    demo_btn = page.locator("#run-demo")
    assert demo_btn.is_visible()
    assert demo_btn.inner_text().startswith("▶")

    # 场景下拉包含全部 11 个场景
    options = page.locator("#scenario-select option").all()
    assert len(options) == 11

    # 运行示例：正常 + 边界，计时必须在 15 秒内
    started = time.perf_counter()
    demo_btn.click()
    page.wait_for_function(
        "document.querySelector('#run-demo').textContent.includes('示例完成')",
        timeout=20000,
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 15, f"示例运行耗时 {elapsed:.1f}s 超过 15 秒要求"

    # 两份结果都在历史里
    history = page.locator("#run-history li").all()
    assert len(history) >= 2

    # 最终展示的是边界场景 clipped：应有裁剪点证据
    page.wait_for_selector("#cnt-clipped")
    clipped_count = int(page.locator("#cnt-clipped").inner_text())
    assert clipped_count >= 1
    page.locator(".tab[data-tab='clipped']").click()
    clip_text = page.locator("#tab-clipped").inner_text()
    assert "裁剪" in clip_text and "不可见" in clip_text

    # 图上叠加了可点击标记
    pins = page.locator(".marker-pin").all()
    assert len(pins) >= 5


def test_click_marker_shows_conversion_trace(browser, server):
    page = _page(browser, server)
    page.locator("#scenario-select").select_option("linear_basic")
    page.locator("#btn-run-sample").click()
    page.wait_for_selector(".marker-pin.ok", timeout=10000)

    page.locator(".marker-pin.ok").first.click()
    page.wait_for_selector("#modal-mask:not(.hidden)")
    body = page.locator("#modal-body").inner_text()
    assert "分步换算" in body
    assert "px" in body and "误差范围" in body
    # 线性轴公式必须出现
    assert "v =" in body

    page.locator("#modal-close").click()
    assert page.locator("#modal-mask").is_hidden()


def test_boundary_scenarios_show_visible_explanations(browser, server):
    page = _page(browser, server)

    # 刻度不足 -> 部分状态 + 可见错误
    page.locator("#scenario-select").select_option("insufficient_ticks")
    page.locator("#btn-run-sample").click()
    page.wait_for_selector(".status-banner.partial", timeout=10000)
    page.locator(".tab[data-tab='errors']").click()
    err_text = page.locator("#tab-errors").inner_text()
    assert "至少需要 2 个" in err_text or "拒绝" in err_text

    # 非法对数 -> 有红点（拒绝给值）
    page.locator("#scenario-select").select_option("illegal_log")
    page.locator("#btn-run-sample").click()
    page.wait_for_selector(".marker-pin.blocked", timeout=10000)
    blocked = page.locator(".marker-pin.blocked").count()
    assert blocked >= 1
    page.locator(".marker-pin.blocked").first.click()
    page.wait_for_selector("#modal-mask:not(.hidden)")
    modal_text = page.locator("#modal-body").inner_text()
    assert "拒绝" in modal_text


def test_protocol_subset_dialog(browser, server):
    page = _page(browser, server)
    page.locator("#btn-protocol").click()
    page.wait_for_selector("#protocol-mask:not(.hidden)")
    text = page.locator("#protocol-body").inner_text()
    assert "支持" in text and "不支持" in text
    assert "clipPath" in text


def test_upload_and_rerun_flow(browser, server, tmp_path):
    page = _page(browser, server)
    # 用生成器产出的 SVG 模拟“外部输入”
    src = os.path.join(ROOT, "samples", "reversed_x.svg")
    upload = tmp_path / "my-chart.svg"
    upload.write_bytes(open(src, "rb").read())
    page.locator("#file-input").set_input_files(str(upload))
    page.locator("#btn-upload").click()
    page.wait_for_selector(".marker-pin.ok", timeout=10000)

    # 修改文件后重新采集：把标题改掉，确认重采走的是磁盘新文件
    content = upload.read_text(encoding="utf-8")
    upload.write_text(content.replace("虚构", "已修改-虚构"), encoding="utf-8")
    # 同步修改服务端保存的副本（模拟用户在 runs/ 里改图）
    stored_name = page.evaluate("state.result.stored_svg")
    stored = os.path.join(ROOT, "runs", stored_name)
    stored_content = open(stored, encoding="utf-8").read()
    with open(stored, "w", encoding="utf-8") as fh:
        fh.write(stored_content.replace("虚构", "已修改-虚构"))
    page.locator("#btn-rerun").click()
    page.wait_for_function(
        "document.querySelector('#run-meta').textContent.includes('任务')",
        timeout=10000,
    )
    # 重采后观测点仍然存在（结构未被破坏）
    assert page.locator(".marker-pin.ok").count() >= 5
