"""端到端验证：真实启动 python3 app.py，用 Playwright 操作中文页面。

覆盖：
- #run-demo 按钮在 15 秒内依次跑完正常与边界两个场景并保留结果；
- 正常场景显示“完整结束”，边界场景显示“流中断（无 end）”且不标完整；
- 快照滑块可回放补丁过程，证据行可点开原始消息；
- 完整验收场景（comprehensive）经选择入口运行，显示“结束但有未决项”；
- 1280×720 视口下关键操作首屏可见。
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pytest.importorskip("playwright")
from playwright.sync_api import expect, sync_playwright  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def server():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "app.py"),
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        out = proc.stdout.read() if proc.stdout else ""
        pytest.fail(f"服务未在 20 秒内就绪：\n{out}")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture()
def page(server):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        pg = browser.new_page(viewport={"width": 1280, "height": 720})
        pg.goto(server)
        pg.wait_for_selector("#run-demo")
        yield pg
        browser.close()


def test_run_demo_completes_within_15_seconds(page):
    started = time.monotonic()
    page.click("#run-demo")

    # 两个任务都出现并跑到终态
    page.wait_for_selector(".status-complete", timeout=15000)
    page.wait_for_selector(".status-interrupted", timeout=15000)
    elapsed = time.monotonic() - started
    assert elapsed < 15, f"示例运行耗时 {elapsed:.1f}s，超过 15 秒目标"

    pills = page.locator(".task-item .status-pill").all_inner_texts()
    assert "完整结束" in pills
    assert "流中断（无 end）" in pills
    # 两个任务结果都保留在列表中
    assert page.locator(".task-item").count() == 2


def test_normal_task_snapshot_playback_and_evidence(page):
    page.click("#run-demo")
    page.wait_for_selector(".status-complete", timeout=15000)
    page.wait_for_selector(".status-interrupted", timeout=15000)

    # 打开正常场景任务（列表中第一个完成的任务）
    page.locator(".task-item", has_text="正常流").first.click()
    # 自动重试：结果面板可能在最后一次轮询渲染前仍显示空态
    expect(page.locator("#result-area")).to_be_visible(timeout=10000)
    expect(page.locator("#tree-view")).to_contain_text("1280", timeout=10000)

    # 结果树包含经重放得到的 metrics.views 与收敛后的引用列表
    tree_text = page.locator("#tree-view").inner_text()
    assert "views" in tree_text and "1280" in tree_text
    assert "REF-1" in tree_text and "REF-2" in tree_text

    # 滑块拖到最后一个快照
    slider = page.locator("#snapshot-range")
    max_no = slider.get_attribute("max")
    assert int(max_no) > 1
    slider.fill(max_no)
    assert "完整结束" in page.locator("#snapshot-label").inner_text()

    # 证据行数 > 0，且包含一条重复拒绝记录；点开可看原始消息
    rows = page.locator(".decision-row")
    assert rows.count() > 5
    page.locator(".decision-row.rejected").first.click()
    page.wait_for_selector("#raw-modal:not(.hidden)")
    raw = page.locator("#raw-content").inner_text()
    assert '"type"' in raw and '"seq"' in raw
    page.click("#modal-close")

    # 状态面板明确显示完整标记
    assert "complete=true" in page.locator("#status-view").inner_text()


def test_interrupted_task_never_marked_complete(page):
    page.click("#run-demo")
    page.wait_for_selector(".status-interrupted", timeout=15000)

    page.locator(".task-item", has_text="边界流").first.click()
    expect(page.locator("#result-area")).to_be_visible(timeout=10000)
    expect(page.locator("#status-view")).to_contain_text("暂存树不完整", timeout=10000)
    status_text = page.locator("#status-view").inner_text()
    # 挂起的 /peer_review/verdict 可见
    assert "/peer_review/verdict" in status_text
    # 所有快照滑块位置都不允许出现 complete=true
    slider = page.locator("#snapshot-range")
    for i in range(int(slider.get_attribute("max")) + 1):
        slider.fill(str(i))
        assert "complete=true" not in page.locator("#status-view").inner_text()


def test_acceptance_scenario_selectable(page):
    # 完整验收场景通过下拉框选择入口运行
    page.select_option("#scenario-select", "comprehensive")
    assert "ended_unresolved" in page.locator("#scenario-desc").inner_text()
    page.click("#run-task")
    page.wait_for_selector(".status-ended_unresolved", timeout=15000)

    page.locator(".task-item", has_text="综合场景").first.click()
    expect(page.locator("#result-area")).to_be_visible(timeout=10000)
    expect(page.locator("#status-view")).to_contain_text("暂存树不完整", timeout=10000)
    status_text = page.locator("#status-view").inner_text()
    assert "/dataset/doi" in status_text and "/peer_review/decision" in status_text
    # 字段错误保留来源 seq
    assert "seq=4" in status_text


def test_key_controls_visible_in_first_screen_at_1280x720(page):
    viewport = page.viewport_size
    assert viewport == {"width": 1280, "height": 720}
    for selector in ("#run-demo", "#scenario-select", "#transport-select", "#run-task"):
        box = page.locator(selector).bounding_box()
        assert box is not None
        assert box["y"] < 720 and box["x"] + box["width"] <= 1280
