# -*- coding: utf-8 -*-
"""端到端验证：Flask API 全链路 + Playwright 浏览器 DOM 采集与页面操作。"""
import json
import os
import subprocess
import sys
import time

import pytest

from collector.fetchers import collect_browser, CollectorError


# ---------------------------------------------------------- Flask API 链路

def test_api_locales_and_samples(app_client):
    data = app_client.get("/api/locales").get_json()
    codes = {c["code"] for c in data["choices"]}
    assert {"en_US", "de_DE", "fr_FR", "sv_SE", "und"} <= codes

    samples = app_client.get("/api/samples").get_json()
    assert samples["source_urls"]["html"] == "/source/measurements.html"


def test_api_run_text_and_persistence(app_client):
    resp = app_client.post("/api/run", json={
        "mode": "text", "locale": "en_US",
        "text": "1,234.56 mg/L\n-\n< 0.01 mg/L\n25.3 zz",
    })
    payload = resp.get_json()
    result = payload["result"]
    assert result["total"] == 4
    statuses = [r["status"] for r in result["records"]]
    assert statuses == ["ok", "missing", "below_LOD", "error"]
    # 结果已持久化
    detail = app_client.get(f"/api/runs/{payload['run_id']}.json").get_json()
    assert detail["run_id"] == payload["run_id"]


def test_api_run_ambiguous_returns_structured_error(app_client):
    resp = app_client.post("/api/run", json={
        "mode": "text", "locale": "und", "text": "1,234",
    })
    rec = resp.get_json()["result"]["records"][0]
    assert rec["error"]["code"] == "AMBIGUOUS_LOCALE"


def test_api_demo_runs_two_scenarios_and_keeps_both(app_client):
    payload = app_client.post("/api/demo").get_json()
    assert len(payload["scenarios"]) == 2
    normal, boundary = payload["scenarios"]
    assert normal["result"]["error_count"] == 0
    # 边界场景：缺失不变 0、检测限不当等值、非法单位报错、歧义报错
    bmap = {r["raw_text"]: r for r in boundary["result"]["records"]}
    assert bmap["-"]["status"] == "missing" and bmap["-"]["value"] is None
    assert bmap["< 0.01 mg/L"]["status"] == "below_LOD"
    assert bmap["25.3 zz"]["error"]["code"] == "ILLEGAL_UNIT"
    assert bmap["1,234"]["error"]["code"] == "AMBIGUOUS_LOCALE"


def test_api_acceptance_covers_five_locales(app_client):
    payload = app_client.post("/api/acceptance").get_json()
    scenarios = {b["scenario"]: b["result"] for b in payload["runs"]}
    locale_scenarios = [k for k in scenarios if k.startswith("歧义原文重解释")]
    assert len(locale_scenarios) == 5


def test_api_file_mode_against_generated_samples(app_client, samples_dir):
    path = os.path.join(samples_dir, "source", "measurements_en.json")
    payload = app_client.post("/api/run", json={
        "mode": "file", "locale": "und", "path": path,
    }).get_json()
    assert payload["result"]["total"] == 24


def test_api_bad_input_returns_400(app_client):
    assert app_client.post("/api/run", json={"mode": "text",
                                             "locale": "en_US", "text": ""}).status_code == 400


def test_source_page_serves_html_with_locale_meta(server_base_url):
    import requests
    html = requests.get(f"{server_base_url}/source/measurements.html", timeout=10).text
    assert 'name="measurement-locale" content="en_US"' in html
    assert 'table id="measurements"' in html


# ---------------------------------------------------------- Playwright 浏览器

def _playwright_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _playwright_available(),
    reason="Playwright Chromium 未安装，跳过浏览器端到端用例",
)


def test_browser_dom_collection_uses_rendered_page(server_base_url):
    records = collect_browser(f"{server_base_url}/source/measurements.html",
                              headless=True)
    assert len(records) == 24
    assert records[0].source_type == "browser_dom"
    assert records[0].declared_locale == "en_US"
    # DOM 中的不换行空格等原文被原样取回
    joined = " ".join(r.raw_value for r in records)
    assert "1,234.56 mg/L" in joined


def test_console_page_run_demo_button_within_15s(server_base_url):
    """验收：run-demo 按钮端到端，15 秒内完成且两个场景结果保留可查看。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(f"{server_base_url}/", wait_until="networkidle")

        assert page.locator("#run-demo").is_visible()
        t0 = time.time()
        page.locator("#run-demo").click()
        page.wait_for_selector(".scenario-title", timeout=15000)
        page.wait_for_function(
            "() => document.querySelectorAll('.scenario-title').length === 2",
            timeout=15000,
        )
        elapsed = time.time() - t0
        assert elapsed < 15.0

        titles = page.locator(".scenario-title").all_inner_texts()
        assert len(titles) == 2
        # 首屏可见关键操作（按钮位于左栏视口内）
        assert page.locator("#run-task").is_visible()
        assert page.locator("#run-demo").bounding_box()["y"] < 720
        # 汇总信息已渲染（两个场景各一个；滚动容器内允许不在当前视口）
        assert page.locator("#results .summary").count() == 2
        # 证据可展开：第一条记录的逐步解析
        page.locator("details.evidence").first.evaluate("el => el.open = true")
        assert page.locator("ol.steps").first.is_visible()
        # 缺失值在页面上明确显示 null 而非 0
        body = page.locator("#results").inner_text()
        assert "null（保留空值）" in body
        assert "AMBIGUOUS_LOCALE" in body
        browser.close()


def test_console_page_locale_switch_reinterprets(server_base_url):
    """页面切换地区规则后同一原文得到不同解释。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(f"{server_base_url}/", wait_until="networkidle")
        page.locator("#text-input").fill("1,234")

        page.locator("#locale").select_option("en_US")
        page.locator("#run-task").click()
        page.wait_for_selector("table.grid")
        assert "1234" in page.locator("#results").inner_text()

        page.locator("#locale").select_option("de_DE")
        page.locator("#run-task").click()
        page.wait_for_function(
            "() => document.querySelector('#results').innerText.includes('1.234')"
        )

        page.locator("#locale").select_option("und")
        page.locator("#run-task").click()
        page.wait_for_function(
            "() => document.querySelector('#results').innerText.includes('AMBIGUOUS_LOCALE')"
        )
        browser.close()
