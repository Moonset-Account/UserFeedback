"""Playwright 浏览器端到端验证。

由 tests/run_e2e.py 启动真实 app 服务后调用本模块；断言页面操作与真实
任务结果，不使用任何模拟数据。通过条件：
- run-demo 按钮在 15 秒内让两个演示任务到达终态并保留结果；
- 页面能展示区间、缺口、对象版本链、真实解析摘要与具体错误证据；
- 完整验收场景可通过下拉入口运行并完成；
- 1280×720 视口下关键操作与结果首屏可见。
"""

from __future__ import annotations

import sys
import time
from typing import Any, Dict

from playwright.sync_api import Page, expect

VIEWPORT = {"width": 1280, "height": 720}
DEMO_BUDGET_SECONDS = 15


def _task_states(page: Page) -> list[Dict[str, Any]]:
    return page.evaluate(
        """async () => {
            const r = await fetch('/api/tasks');
            const d = await r.json();
            return d.tasks.map(t => ({
                id: t.task_id, label: t.label, status: t.status,
                rejected: t.rejected_count, hasResult: !!t.result,
                versions: (t.version_chain || []).length,
            }));
        }"""
    )


def test_demo_button_completes_in_budget(page: Page, base_url: str) -> None:
    page.goto(base_url)
    page.set_viewport_size(VIEWPORT)
    expect(page.locator("#run-demo")).to_be_visible()

    # 首屏可见性：关键操作与结果面板无需滚动
    assert page.locator("#create-task").is_visible()
    assert page.locator("#scenario-select").is_visible()
    assert page.locator("#detail-title").is_visible()

    start = time.time()
    page.locator("#run-demo").click()

    # 等待两个演示任务都进入终态，预算 15 秒
    deadline = start + DEMO_BUDGET_SECONDS
    states: list[Dict[str, Any]] = []
    while time.time() < deadline:
        states = _task_states(page)
        if len(states) >= 2 and all(
            t["status"] in ("complete", "failed", "stopped") for t in states
        ):
            break
        page.wait_for_timeout(300)
    elapsed = time.time() - start

    assert len(states) >= 2, "演示应创建两个任务"
    terminal = {"complete", "failed", "stopped"}
    assert all(t["status"] in terminal for t in states), states
    assert elapsed <= DEMO_BUDGET_SECONDS, f"演示耗时 {elapsed:.1f}s 超过 15s 预算"
    print(f"[e2e] 演示两个任务在 {elapsed:.1f}s 内结束")

    # 两个任务都必须真实解析成功（正常续取 + 版本变更后重建）
    assert all(t["hasResult"] for t in states), states
    labels = {t["label"] for t in states}
    assert any("正常续取" in label for label in labels)
    assert any("边界" in label for label in labels)
    # 边界任务经历了版本切换，版本链应有两个强校验器
    boundary = next(t for t in states if "边界" in t["label"])
    assert boundary["versions"] == 2, boundary
    assert boundary["rejected"] >= 1, boundary


def _expand_evidence(page: Page) -> None:
    details = page.locator(".evidence")
    if not details.evaluate("el => el.open"):
        details.locator("summary").click()
    expect(page.locator("#event-log .event-item").first).to_be_visible()


def test_detail_shows_intervals_versions_and_evidence(page: Page,
                                                      base_url: str) -> None:
    page.goto(base_url)
    page.set_viewport_size(VIEWPORT)
    page.locator("#run-demo").click()
    page.wait_for_function(
        """async () => {
            const r = await fetch('/api/tasks');
            const d = await r.json();
            return d.tasks.length >= 2 && d.tasks.every(
                t => ['complete','failed','stopped'].includes(t.status));
        }""",
        timeout=15000,
    )
    # 点击第一张任务卡，检查详情渲染
    page.locator(".task-card").first.click()
    expect(page.locator("#detail-body")).to_be_visible()
    expect(page.locator("#badge-status.complete")).to_be_visible()
    # 覆盖条、区间、版本链、解析结果
    expect(page.locator("#coverage-bar .seg-covered").first).to_be_visible()
    expect(page.locator("#spans-list li").first).to_be_visible()
    expect(page.locator("#version-list .etag").first).to_be_visible()
    expect(page.locator("#result-box")).to_be_visible()
    expect(page.locator("#result-grid .result-cell")).to_have_count(10)
    # 证据默认折叠，展开后应含真实 HTTP 事件
    _expand_evidence(page)
    assert page.locator(".event-item").count() >= 3


def _wait_fresh_task_terminal(page: Page, before_ids: set[str],
                               timeout_ms: int = 25000) -> Dict[str, Any]:
    """轮询等待在 before_ids 之后新建的任务到达终态，并返回其最新状态。"""
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        fresh = [t for t in _task_states(page) if t["id"] not in before_ids]
        if fresh and fresh[0]["status"] in ("complete", "failed", "stopped"):
            return fresh[0]
        page.wait_for_timeout(300)
    fresh = [t for t in _task_states(page) if t["id"] not in before_ids]
    raise AssertionError(f"新建任务未在预算内结束: {fresh[:1]}")


def test_full_acceptance_scenario_selector(page: Page, base_url: str) -> None:
    page.goto(base_url)
    page.set_viewport_size(VIEWPORT)
    before_ids = {t["id"] for t in _task_states(page)}
    # 选择“服务器错误区间”完整验收场景并运行
    page.locator("#scenario-select").select_option("error-region")
    page.locator("#create-task").click()
    newest = _wait_fresh_task_terminal(page, before_ids)
    assert newest["status"] == "complete", newest
    assert newest["hasResult"] is True
    assert newest["rejected"] >= 3, newest
    # 错误区间被拒绝，展开证据后应能看到 server-error / 416 事件
    page.locator(".task-card").first.click()
    _expand_evidence(page)
    body = page.locator("#event-log").inner_text()
    assert "server-error" in body
    assert "range-unsatisfiable" in body


def test_bad_content_range_is_rejected_then_recovers(page: Page,
                                                     base_url: str) -> None:
    page.goto(base_url)
    page.set_viewport_size(VIEWPORT)
    before_ids = {t["id"] for t in _task_states(page)}
    page.locator("#scenario-select").select_option("bad-content-range")
    page.locator("#create-task").click()
    newest = _wait_fresh_task_terminal(page, before_ids)
    assert newest["status"] == "complete", newest
    page.locator(".task-card").first.click()
    _expand_evidence(page)
    body = page.locator("#event-log").inner_text()
    # 损坏响应被明确拒绝，且最终仍解析成功
    assert "inconsistent-total-length" in body
    expect(page.locator("#result-box")).to_be_visible()


def run_all(page: Page, base_url: str) -> int:
    failures = 0
    for fn in (
        test_demo_button_completes_in_budget,
        test_detail_shows_intervals_versions_and_evidence,
        test_full_acceptance_scenario_selector,
        test_bad_content_range_is_rejected_then_recovers,
    ):
        try:
            fn(page, base_url)
            print(f"[e2e] PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[e2e] FAIL {fn.__name__}: {exc}", file=sys.stderr)
    return failures
