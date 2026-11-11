"""Playwright 浏览器采集与执行器。

采集边界（硬性约束）：
- 只允许访问同源的 /lab/ 页面与 /lab/api/snapshot、/lab/api/health 两个公开只读接口
  以及 /lab/static/ 下的静态资源；
- 任何离开该边界的请求都会被路由中止并记录 COLLECTOR_BOUNDARY_VIOLATION；
- 不读取仅供测试的独立真值目录，不使用 page.evaluate 读取 JS 内部业务变量，
  菜单与状态一律来自渲染后的 DOM 文本/属性或公开快照接口。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import (
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .errors import BrowserInteractionError, CollectorBoundaryError, StateVerificationError
from .menu_model import MenuNode, MenuSnapshot
from .resolver import Decision, resolve_command

_ALLOWED_API_PATHS = {
    "/lab/api/snapshot",
    "/lab/api/health",
    "/lab/api/context",
    "/lab/api/command",
    "/lab/api/reset",
}


@dataclass
class StepEvidence:
    index: int
    action: str
    summary: str
    decision: dict | None = None
    observed: dict | None = None
    screenshot: str | None = None
    blocked_requests: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "action": self.action,
            "summary": self.summary,
            "decision": self.decision,
            "observed": self.observed,
            "screenshot": self.screenshot,
            "blocked_requests": self.blocked_requests,
            "note": self.note,
        }


class LabSession:
    """一次 RPA 任务对应的浏览器会话，线程内独占一个 Playwright 实例。"""

    def __init__(
        self,
        base_url: str,
        evidence_dir: Path,
        *,
        headless: bool = True,
        timeout_ms: int = 8000,
    ):
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise CollectorBoundaryError(f"base_url 非法: {base_url}")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.lab_url = f"{self.origin}/lab/"
        self.evidence_dir = evidence_dir
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._pw: Playwright | None = None
        self._page: Page | None = None
        self._shot_seq = 0
        self._lock = threading.Lock()

    # ---- 生命周期 -------------------------------------------------
    def __enter__(self) -> "LabSession":
        self._pw = sync_playwright().start()
        browser = self._pw.chromium.launch(headless=self.headless)
        context = browser.new_context(
            viewport={"width": 1280, "height": 720}, locale="zh-CN"
        )
        context.route("**/*", self._route_guard)
        self._page = context.new_page()
        self._page.set_default_timeout(self.timeout_ms)
        self._page.goto(self.lab_url, wait_until="domcontentloaded")
        self._page.wait_for_selector("#lab-app", state="visible")
        # 建立干净基线：复位实验台虚构状态，再重新加载页面使 DOM 与复位后快照一致。
        self._page.request.post(f"{self.origin}/lab/api/reset")
        self._page.reload(wait_until="domcontentloaded")
        self._page.wait_for_selector("#lab-app", state="visible")
        self._page.wait_for_function(
            "() => document.querySelector('#current-context')?.textContent"
            " === 'ALPHA 标准实验'"
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._page is not None:
                ctx = self._page.context
                ctx.browser.close()
                ctx.close()
        finally:
            if self._pw is not None:
                self._pw.stop()
            self._pw = None
            self._page = None

    def _route_guard(self, route) -> None:
        url = urlparse(route.request.url)
        request_origin = f"{url.scheme}://{url.netloc}"
        same_origin = request_origin == self.origin
        static_ok = same_origin and url.path.startswith("/lab/static/")
        page_ok = same_origin and url.path == "/lab/"
        api_ok = same_origin and url.path in _ALLOWED_API_PATHS
        if static_ok or page_ok or api_ok:
            route.continue_()
            return
        # 越界请求：文档类导航返回 403（避免 abort 把整页加载拖到超时），
        # 静态子资源中止加载；两者都在页面侧记录（仅写 window 日志数组）。
        self._record_blocked(route.request.url)
        if route.request.resource_type in ("document", "xhr", "fetch"):
            route.fulfill(
                status=403,
                content_type="application/json",
                body='{"error": "blocked by RPA collector boundary"}',
            )
        else:
            route.abort()

    def _record_blocked(self, url: str) -> None:
        if self._page is None:
            return
        try:
            self._page.evaluate(
                "(u) => { window.__rpaBlocked = window.__rpaBlocked || [];"
                " if (!window.__rpaBlocked.includes(u)) window.__rpaBlocked.push(u); }",
                url,
            )
        except Exception:
            pass

    # ---- 基础采集 -------------------------------------------------
    def fetch_snapshot(self) -> MenuSnapshot:
        """通过公开只读接口采集菜单快照（不接触真值文件）。"""
        try:
            response = self._page.request.get(f"{self.origin}/lab/api/snapshot")
        except PlaywrightTimeoutError as exc:
            raise BrowserInteractionError("快照接口请求超时") from exc
        if not response.ok:
            raise BrowserInteractionError(
                f"快照接口返回 HTTP {response.status}",
                detail={"url": f"{self.origin}/lab/api/snapshot"},
            )
        return MenuSnapshot.from_dict(response.json())

    def _screenshot(self, tag: str) -> str:
        self._shot_seq += 1
        safe_tag = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in tag)
        name = f"step{self._shot_seq:02d}-{safe_tag}.png"
        self._page.screenshot(path=str(self.evidence_dir / name), full_page=False)
        return name

    def screenshot(self, tag: str) -> str:
        with self._lock:
            return self._screenshot(tag)

    def blocked_requests(self) -> list[str]:
        try:
            return self._page.evaluate("window.__rpaBlocked || []")
        except Exception:
            return []

    # ---- DOM 交互原语（一律按稳定 id/data-* 定位，不用列表序号） --
    def switch_context(self, context: str) -> dict:
        page = self._page
        expected_label = {
            "ALPHA": "ALPHA 标准实验",
            "BETA": "BETA 极限实验",
        }[context]
        # 目标上下文与当前一致时 select 不会触发 change，直接确认 DOM 即可。
        if self.text("#current-context") == expected_label:
            return {"context": context, "visible": expected_label, "changed": False}
        page.locator("#context-switcher").select_option(context)
        page.wait_for_function(
            "(label) => document.querySelector('#current-context')?.textContent === label",
            arg=expected_label,
        )
        return {"context": context, "visible": expected_label, "changed": True}

    def search(self, query: str, limit: int) -> dict:
        page = self._page
        page.keyboard.press("Control+Shift+P")
        page.wait_for_selector("#palette-overlay", state="visible")
        page.locator("#palette-input").fill(query)
        # 等待动态排序稳定：结果行 data-id 序列连续两次采样一致（轮询，不依赖事件）。
        def signature():
            return page.eval_on_selector_all(
                "#palette-results .palette-row",
                "els => els.map(e => e.dataset.id).join('|')",
            )

        last = object()
        stable_rounds = 0
        for _ in range(40):  # 最多 ~4s
            current = signature()
            if current == last:
                stable_rounds += 1
                if stable_rounds >= 2 and current != "":
                    break
            else:
                stable_rounds = 0
                last = current
            page.wait_for_timeout(100)
        rows = page.locator("#palette-results .palette-row").all()
        items = []
        for row in rows[:limit]:
            items.append(
                {
                    "id": row.get_attribute("data-id"),
                    "label": row.locator(".row-label").inner_text(),
                    "path": row.locator(".row-path").inner_text(),
                    "enabled": row.get_attribute("data-enabled") == "true",
                    "score_text": row.locator(".row-score").inner_text(),
                }
            )
        shot = self._screenshot("search")
        page.keyboard.press("Escape")
        page.wait_for_selector("#palette-overlay", state="hidden")
        return {"query": query, "ordered_results": items, "screenshot": shot}

    def open_path(self, decision: Decision) -> dict:
        """按完整菜单路径逐级展开并点击最终命令，定位只使用 data-id/data-path。"""
        page = self._page
        node = decision.node
        assert node is not None

        # 1) 点击菜单栏根项。
        page.locator(
            f"#menubar .menu-root[data-label='{_css(node.path[0])}']"
        ).click()

        # 2) 逐段悬停展开子菜单；动态子菜单等待加载占位消失。
        for depth in range(1, len(node.path) - 1):
            partial = node.path[: depth + 1]
            item = page.locator(
                ".menu-popup .menu-item:visible"
                f"[data-path='{_css(' > '.join(partial))}']"
            ).first
            item.wait_for(state="visible")
            item.hover()
            loading = page.locator(
                ".menu-popup .menu-item:visible"
                f"[data-path='{_css(' > '.join(partial))}'] .dynamic-loading"
            )
            if loading.count() > 0:
                loading.first.wait_for(state="detached", timeout=4000)

        # 3) 点击最终命令：仅匹配 data-id 且 data-enabled=true 的可见元素。
        final = page.locator(
            ".menu-popup .menu-item:visible"
            f"[data-id='{_css(node.id)}'][data-enabled='true']"
        ).first
        final.wait_for(state="visible")
        final.click()
        page.wait_for_selector(".menu-popup", state="detached", timeout=4000)
        return {"clicked_node_id": node.id, "clicked_path": list(node.path)}

    def read_state(self, target: str) -> dict:
        selector = self._target_selector(target)
        return {"target": target, "text": self.text(selector)}

    def wait_visible_text(self, target: str, timeout_ms: int) -> dict:
        selector = self._target_selector(target)
        try:
            self._page.locator(selector).first.wait_for(
                state="visible", timeout=timeout_ms
            )
        except PlaywrightTimeoutError as exc:
            raise BrowserInteractionError(
                f"等待 {target} 可见超时（{timeout_ms}ms）"
            ) from exc
        return {"target": target, "text": self.text(selector)}

    def expect_state(self, target: str, equals: str) -> dict:
        observed = self.read_state(target)
        if observed["text"] != equals:
            shot = self._screenshot("verify-fail")
            raise StateVerificationError(
                f"状态校验失败：{target} 期望 {equals!r}，实际 {observed['text']!r}",
                detail={"expected": equals, "observed": observed, "screenshot": shot},
            )
        return observed

    def text(self, selector: str) -> str:
        return self._page.locator(selector).first.inner_text().strip()

    @staticmethod
    def _target_selector(target: str) -> str:
        if target.startswith("device:"):
            name = target.split(":", 1)[1]
            return f"#device-card-{_css(name)} .device-state"
        if target == "preset":
            return "#current-preset"
        if target == "context":
            return "#current-context"
        raise BrowserInteractionError(f"未知 state target: {target}")

    # ---- 供执行器调用的高层封装 -----------------------------------
    def resolve_live(self, menu_path, scope):
        snapshot = self.fetch_snapshot()
        return snapshot, resolve_command(snapshot, menu_path, scope)

    def expand_dynamic_then_resolve(self, menu_path, scope):
        """路径经过动态子菜单时：先在浏览器中展开父菜单，再重新采集并决策。"""
        page = self._page
        # 1) 打开根菜单并逐级悬停到动态节点的父级。
        root_label = menu_path[0]
        page.locator(
            f"#menubar .menu-root[data-label='{_css(root_label)}']"
        ).click()
        dynamic_parent_path = list(menu_path[:-1])
        for depth in range(1, len(dynamic_parent_path)):
            partial = dynamic_parent_path[: depth + 1]
            item = page.locator(
                ".menu-popup .menu-item:visible"
                f"[data-path='{_css(' > '.join(partial))}']"
            ).first
            item.wait_for(state="visible")
            item.hover()
        # 2) 等待动态加载占位消失，且预设命令行渲染出来。
        dynamic_selector = (
            ".menu-popup .menu-item:visible"
            f"[data-path='{_css(' > '.join(dynamic_parent_path))}']"
        )
        loading = page.locator(f"{dynamic_selector} .dynamic-loading")
        if loading.count() > 0:
            loading.first.wait_for(state="detached", timeout=4000)
        # 预设命令渲染在动态节点的弹出层中（data-scope=preset）。
        page.locator(".menu-popup .menu-item[data-scope='preset']").first.wait_for(
            state="visible", timeout=4000
        )
        # 3) 从渲染后的 DOM 采集已加载的预设命令，构造增强快照后重新决策。
        preset_rows = page.eval_on_selector_all(
            ".menu-popup .menu-item[data-scope='preset']",
            "els => els.map(e => ({id: e.dataset.id, path: e.dataset.path.split(' > '),"
            " label: e.querySelector('.item-label').textContent, enabled: e.dataset.enabled === 'true'}))",
        )
        snapshot = self.fetch_snapshot()
        dynamic_node = next(
            n for root in snapshot.roots for n in root.all_nodes() if n.id == "dynamic.presets"
        )
        children = tuple(
            MenuNode(
                id=row["id"],
                label=row["label"],
                path=tuple(row["path"]),
                scope="preset",
                kind="command",
                enabled=row["enabled"],
                shortcut=None,
            )
            for row in preset_rows
        )
        object.__setattr__(dynamic_node, "dynamic_loaded", True)
        object.__setattr__(dynamic_node, "children", children)
        refreshed = MenuSnapshot(
            context=snapshot.context,
            roots=snapshot.roots,
            shortcuts=snapshot.shortcuts,
            states=snapshot.states,
        )
        # 收起菜单，决策与点击由 open_path 重新走一遍完整路径（保证证据一致）。
        page.keyboard.press("Escape")
        page.wait_for_selector(".menu-popup", state="detached", timeout=4000)
        return refreshed, resolve_command(refreshed, menu_path, scope)

    @property
    def page(self) -> Page:
        assert self._page is not None
        return self._page


def _css(value: str) -> str:
    """转义 CSS 属性选择器中的反斜杠与单引号。"""
    return value.replace("\\", "\\\\").replace("'", "\\'")
