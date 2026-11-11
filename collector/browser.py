"""基于 Playwright 的浏览器采集通道（可选）。

适用场景：目录页依赖 JavaScript 渲染、或验收要求真实浏览器交互。
预算口径与 collector.scanner 完全一致：**每次页面导航（含重试）计 1 次请求**。
解析仍复用 collector.parsers 的 HTML 协议子集（读取渲染后的 DOM outerHTML），
因此浏览器通道不引入任何额外的解析特权，也不读取本地真值。

使用前需安装：
    pip install playwright && python -m playwright install chromium
未安装时构造/运行会给出明确中文错误，而不是隐式降级。
"""
from __future__ import annotations

from typing import Callable

from .parsers import ParsedPage, parse_page
from .scanner import (
    CatalogScanner,
    PageEvent,
    ScanError,
    ScanReport,
    StopEvent,
    _BudgetExhausted,
)


class BrowserUnavailable(RuntimeError):
    """Playwright 或浏览器内核未安装。"""


def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception as exc:  # pragma: no cover - 取决于运行环境
        raise BrowserUnavailable(
            "未安装 Playwright。请执行：pip install playwright && python -m playwright install chromium"
        ) from exc
    return sync_playwright


class BrowserCatalogScanner(CatalogScanner):
    """与 CatalogScanner 同构，仅把 HTTP GET 换成 Chromium 页面导航。

    （显式 __init__ 而非 dataclass 继承，避免 dataclass 字段顺序约束。）
    """

    def __init__(
        self,
        start_url: str,
        request_budget: int,
        *,
        timeout: float = 8.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.2,
        session=None,
        event_callback=None,
        sleep: Callable[[float], None] = None,
        headless: bool = True,
        external_browser=None,
    ) -> None:
        import time as _time

        super().__init__(
            start_url, request_budget, timeout=timeout, max_retries=max_retries,
            backoff_seconds=backoff_seconds, session=session,
            event_callback=event_callback, sleep=sleep or _time.sleep,
        )
        self.headless = headless
        # 可注入已启动的 Chromium 浏览器（测试/同一 Playwright 连接内复用时必填），
        # 为 None 时 scan() 自行创建并关闭浏览器。
        self.external_browser = external_browser

    def scan(self) -> ScanReport:  # type: ignore[override]
        self._requests_used = 0
        pages: list[PageEvent] = []
        seen: set[str] = set()
        ordered_unique = []
        total_raw = 0
        url: str | None = self.start_url
        page_index = 0

        owns_browser = self.external_browser is None
        if owns_browser:
            sync_playwright = _import_playwright()
            pw_cm = sync_playwright()
            pw = pw_cm.start()
            try:
                browser = pw.chromium.launch(headless=self.headless)
            except Exception as exc:  # pragma: no cover - 取决于运行环境
                pw_cm.__exit__(None, None, None)
                raise BrowserUnavailable(
                    f"Chromium 启动失败，请执行 python -m playwright install chromium。原始错误: {exc}"
                ) from exc
        else:
            pw_cm = None
            browser = self.external_browser

        try:
            page = browser.new_page()
            while url is not None:
                if self._requests_used >= self.budget:
                    raise _BudgetExhausted(url)
                self._requests_used += 1
                last_exc: Exception | None = None
                for attempt in range(self.max_retries + 1):
                    try:
                        resp = page.goto(url, timeout=self.timeout * 1000, wait_until="domcontentloaded")
                        break
                    except Exception as exc:  # playwright 的 TimeoutError 等
                        last_exc = exc
                        if attempt < self.max_retries:
                            self._sleep(self.backoff_seconds * (attempt + 1))
                            continue
                        raise ScanError(f"浏览器导航失败: {url} :: {exc}") from exc
                else:  # pragma: no cover - 循环正常 break 不会走到
                    raise ScanError(f"浏览器导航失败: {url} :: {last_exc}")

                if resp is not None and not resp.ok:
                    raise ScanError(f"浏览器收到 HTTP {resp.status}: {url}")

                html = page.content()
                parsed: ParsedPage = parse_page(html.encode("utf-8"), "text/html; charset=utf-8", url)

                page_index += 1
                unique_here, dup_here, page_seen = 0, 0, set()
                for rec in parsed.records:
                    total_raw += 1
                    if rec.specimen_id in page_seen:
                        dup_here += 1
                        continue
                    page_seen.add(rec.specimen_id)
                    if rec.specimen_id in seen:
                        dup_here += 1
                    else:
                        seen.add(rec.specimen_id)
                        ordered_unique.append(rec)
                        unique_here += 1

                evt = PageEvent(
                    page_index=page_index,
                    url=url,
                    http_status=resp.status if resp else 0,
                    raw_count=len(parsed.records),
                    unique_on_page=unique_here,
                    duplicate_on_page=dup_here,
                    requests_used=self._requests_used,
                    records=list(parsed.records),
                )
                pages.append(evt)
                if self._event_callback:
                    self._event_callback(evt)
                url = parsed.next_url

            stop = StopEvent(
                reason="completed",
                detail="浏览器已到达末页，目录扫描完整",
                last_page_index=page_index,
                requests_used=self._requests_used,
                budget=self.budget,
            )
            return self._finish(pages, ordered_unique, total_raw, stop, self.start_url)
        except _BudgetExhausted as exc:
            stop = StopEvent(
                reason="budget_exhausted",
                detail=(
                    f"请求预算 {self.budget} 次已用尽，扫描在第 {page_index + 1} 页前中止；"
                    "抽样仅针对已观察集合，不构成全站无偏样本"
                ),
                last_page_index=page_index,
                requests_used=self._requests_used,
                budget=self.budget,
                next_url_not_fetched=exc.url,
            )
            return self._finish(pages, ordered_unique, total_raw, stop, self.start_url)
        finally:
            if owns_browser:
                browser.close()
                pw_cm.__exit__(None, None, None)
