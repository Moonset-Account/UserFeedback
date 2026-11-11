"""基于 Playwright 的浏览器采集驱动（Chromium，同步 API）。

采集只通过浏览器公开交互进行：导航到页面后按契约中的 CSS 选择器读取元素文本，
并保存截图与表格 HTML 片段作为原始证据。驱动本身不做任何语义判断，也不读取
任何“真值”文件。

支持的选择器子集（Playwright CSS 引擎）：
  - id / class / 标签组合，如 table#measure、th.col-dry、td.col-sample
  - 后代组合符（空格），如 table#measure tbody tr
不使用 XPath、文本伪类或自定义脚本注入。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from .contracts import Contract

NAV_TIMEOUT_MS = 15_000
SELECTOR_TIMEOUT_MS = 5_000


@dataclass
class RawPage:
    """一次浏览器采集得到的原始证据。"""

    url: str
    page_title: str
    table_found: bool
    row_count: int
    headers: dict[str, str | None]              # field_key -> 列头文本（未命中为 None）
    rows: list[dict[str, str | None]]           # 每行 field_key -> 单元格文本
    table_html: str
    screenshot_path: str | None
    collected_at: str
    access: str                                 # browser / file

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "page_title": self.page_title,
            "table_found": self.table_found,
            "row_count": self.row_count,
            "headers": self.headers,
            "rows": self.rows,
            "table_html": self.table_html,
            "screenshot_path": self.screenshot_path,
            "collected_at": self.collected_at,
            "access": self.access,
        }


class BrowserDriver:
    """浏览器驱动。

    Playwright 同步 API 具有线程亲和性（greenlet 绑定启动线程），而 Web 请求可能
    落在 Werkzeug 的不同工作线程上，因此这里按线程持有独立的 Playwright/浏览器实例，
    每个线程内部复用同一浏览器进程，线程结束或 close() 时统一回收。
    """

    def __init__(self) -> None:
        self._local = threading.local()
        self._lock = threading.Lock()
        self._all_playwright: list[Any] = []
        self._all_browsers: list[Any] = []

    def __enter__(self) -> "BrowserDriver":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _resources(self) -> tuple[Any, Any]:
        pw = getattr(self._local, "playwright", None)
        browser = getattr(self._local, "browser", None)
        if browser is None or not browser.is_connected():
            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            self._local.playwright = pw
            self._local.browser = browser
            with self._lock:
                self._all_playwright.append(pw)
                self._all_browsers.append(browser)
        return pw, browser

    def start(self) -> None:
        self._resources()

    def close(self) -> None:
        with self._lock:
            for browser in self._all_browsers:
                try:
                    browser.close()
                except Exception:
                    pass
            for pw in self._all_playwright:
                try:
                    pw.stop()
                except Exception:
                    pass
            self._all_browsers.clear()
            self._all_playwright.clear()
        self._local = threading.local()

    def collect(self, url: str, contract: Contract, screenshot_path: str | None = None) -> RawPage:
        _, browser = self._resources()
        page = browser.new_page()
        try:
            page.set_default_timeout(SELECTOR_TIMEOUT_MS)
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            page.wait_for_selector(contract.table_selector, timeout=SELECTOR_TIMEOUT_MS)

            title = page.title()
            table_el = page.query_selector(contract.table_selector)
            table_found = table_el is not None
            table_html = table_el.evaluate("el => el.outerHTML") if table_el else ""

            headers: dict[str, str | None] = {}
            for cf in contract.fields:
                el = page.query_selector(cf.header_selector)
                headers[cf.key] = el.text_content().strip() if el and el.text_content() else (
                    el.text_content() if el else None
                )

            row_els = page.query_selector_all(contract.row_selector)
            rows: list[dict[str, str | None]] = []
            for row_el in row_els:
                row: dict[str, str | None] = {}
                for cf in contract.fields:
                    cell = row_el.query_selector(cf.cell_selector)
                    text = cell.text_content() if cell else None
                    row[cf.key] = text.strip() if isinstance(text, str) else text
                rows.append(row)

            if screenshot_path:
                Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=screenshot_path, full_page=True)
        finally:
            page.close()

        return RawPage(
            url=url,
            page_title=title,
            table_found=table_found,
            row_count=len(rows),
            headers=headers,
            rows=rows,
            table_html=table_html,
            screenshot_path=screenshot_path,
            collected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            access="file" if url.startswith("file://") else "browser",
        )
