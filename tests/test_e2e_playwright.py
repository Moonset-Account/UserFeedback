"""Playwright 端到端验证：真实浏览器操作中文网页。

若本机未安装 Chromium，本模块会自动执行 `python -m playwright install chromium`。
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from .conftest import manifest_url

playwright = pytest.importorskip("playwright")


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            yield browser
            browser.close()
    except Exception as exc:  # 浏览器二进制缺失时自动安装后重试
        if "Executable doesn't exist" not in str(exc) and "playwright install" not in str(exc):
            raise
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
            timeout=300,
        )
        with sync_playwright() as p:
            browser = p.chromium.launch()
            yield browser
            browser.close()


@pytest.fixture()
def page(browser):
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    yield page
    page.close()


def test_demo_button_runs_both_scenarios_within_15s(server, page):
    base_url = server["base_url"]
    page.goto(base_url + "/")

    # 首屏关键元素可见
    assert page.is_visible("#run-demo")
    assert page.is_visible("#source-input")
    page.wait_for_selector(".source-item", timeout=10000)
    assert page.locator(".source-item").count() == 3

    start = time.time()
    page.click("#run-demo")
    page.wait_for_selector("#result-view:not(.hidden)", timeout=15000)
    # 两次运行记录均保留
    page.wait_for_function(
        "document.querySelectorAll('#run-list li').length >= 2", timeout=10000
    )
    elapsed = time.time() - start
    assert elapsed < 15, f"示例运行耗时 {elapsed:.1f}s 超过 15 秒目标"

    # 第一条结果为正常场景：状态 ok，统计数字真实渲染
    run_items = page.locator("#run-list li")
    assert run_items.count() >= 2
    run_items.first.click()
    page.wait_for_selector(".canvas-card", timeout=5000)
    assert page.locator(".canvas-card").count() == 4
    assert page.locator(".range-tree .range-node").count() == 3
    assert "成功" in page.inner_text("#status-banner")

    # 切换到边界场景结果
    run_items.nth(1).click()
    page.wait_for_function(
        "document.querySelectorAll('.cyclic-mark').length >= 2", timeout=8000
    )
    body_text = page.inner_text("body")
    assert "循环引用" in body_text
    assert "循环章节 A" in body_text and "循环章节 B" in body_text
    assert "缺描述" in body_text
    # 问题表给出具体 JSON 路径
    issue_paths = page.locator("#issues-table tbody td.path-text").all_inner_texts()
    assert any(p.startswith("/structures/") for p in issue_paths)
    # 即使部分失败，可用页面仍展示
    assert page.locator(".canvas-card").count() == 3


def test_custom_input_full_acceptance(server, page):
    base_url = server["base_url"]
    page.goto(base_url + "/")
    page.wait_for_selector(".source-item", timeout=10000)
    page.fill("#source-input", manifest_url(base_url, "full-acceptance"))
    page.click("#run-custom")
    page.wait_for_selector("#result-view:not(.hidden)", timeout=10000)
    assert page.locator(".canvas-card").count() == 6
    body_text = page.inner_text("body")
    assert "自循环章节" in body_text
    # Choice 候选与缺描述标签同时出现
    assert "Choice 候选" in body_text
    assert "缺描述" in body_text


def test_bad_input_shows_specific_error(server, page):
    base_url = server["base_url"]
    page.goto(base_url + "/")
    page.wait_for_selector(".source-item", timeout=10000)
    page.fill("#source-input", base_url + "/sample/manifest/does-not-exist.json")
    page.click("#run-custom")
    # 采集器把获取失败记为 status=failed 的任务，页面展示失败横幅与具体错误
    page.wait_for_selector("#result-view:not(.hidden)", timeout=8000)
    page.wait_for_function(
        "document.querySelector('#status-banner').textContent.includes('失败')", timeout=8000
    )
    body_text = page.inner_text("body")
    assert "FETCH_FAILED" in body_text
    assert "404" in body_text
    assert "does-not-exist.json" in body_text
    # 失败任务同样保留在运行记录中
    assert page.locator("#run-list li .badge-failed").count() >= 1


def test_thumbnail_images_load(server, page):
    base_url = server["base_url"]
    page.goto(base_url + "/")
    page.wait_for_selector(".source-item", timeout=10000)
    page.click("#run-demo")
    page.wait_for_selector(".canvas-card img", timeout=15000)
    # 至少一张缩略图真实加载成功（naturalWidth > 0）
    ok = page.evaluate(
        "Array.from(document.querySelectorAll('.canvas-card img')).some(i => i.naturalWidth > 0)"
    )
    assert ok
