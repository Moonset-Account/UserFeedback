"""Playwright 浏览器协议后端。

支持的协议子集：
  Chromium 无头模式；导航后等待 JS 注入 DOM；读取 a#download-link 锚点、
  #business-version 与 #experiment-id 文本；通过浏览器同源 request 上下文下载
  （自动携带页面会话语义），原样保留状态码、响应头与字节。
"""
from __future__ import annotations

from urllib.parse import urljoin

from collector.models import AcquiredLink, AcquireError, BackendError, DownloadResponse

MAX_BYTES = 20 * 1024 * 1024


class PlaywrightBackend:
    name = "playwright"

    def __init__(self, headless: bool = True, action_timeout_ms: int = 15000) -> None:
        self._headless = headless
        self._timeout = action_timeout_ms
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def _ensure_started(self) -> None:
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BackendError(
                "未安装 Playwright，请先执行：python3 -m playwright install chromium"
            ) from exc
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=self._headless)
            self._context = self._browser.new_context(
                user_agent="ExpiringAssetCollector/1.0 (playwright; local fictional demo)",
                accept_downloads=False,
            )
            self._context.set_default_timeout(self._timeout)
            self._page = self._context.new_page()
        except Exception as exc:  # 浏览器缺失/启动失败统一转成后端错误
            self.close()
            raise BackendError(
                f"Chromium 启动失败: {exc}；可执行 python3 -m playwright install chromium 安装浏览器。"
            ) from exc

    def acquire_link(self, page_url: str) -> AcquiredLink:
        self._ensure_started()
        evidence: dict = {"page_url": page_url, "method": "browser-dom-after-js"}
        try:
            resp = self._page.goto(page_url, wait_until="networkidle", timeout=self._timeout)
            evidence["status_code"] = resp.status if resp else None
            # 等待 JS 把下载锚点注入 DOM（服务端渲染页面则立即可见）
            self._page.wait_for_selector("a#download-link[href]", timeout=self._timeout)
            anchor = self._page.locator("a#download-link")
            href = anchor.get_attribute("href")
            anchor_html = anchor.evaluate("el => el.outerHTML")
            version = self._page.locator("#business-version").inner_text().strip()
            experiment_id = self._page.locator("#experiment-id").inner_text().strip()
        except BackendError:
            raise
        except Exception as exc:
            raise AcquireError(f"浏览器渲染或 DOM 等待失败: {exc}", evidence) from exc

        if not href or not version or not experiment_id:
            raise AcquireError("渲染后仍缺少下载地址、版本号或实验编号", evidence)

        download_url = urljoin(page_url, href)
        evidence.update({
            "anchor_html": str(anchor_html)[:300],
            "version_text": version,
            "experiment_id": experiment_id,
            "download_url": download_url,
            "rendered_by_javascript": True,
        })
        return AcquiredLink(download_url, version, experiment_id, evidence)

    def download(self, url: str) -> DownloadResponse:
        self._ensure_started()
        try:
            resp = self._context.request.get(
                url, timeout=self._timeout, max_redirects=5, fail_on_status_code=False
            )
            body = resp.body()
            if len(body) > MAX_BYTES:
                raise BackendError(f"响应体超过 {MAX_BYTES} MiB 上限，中止下载")
            return DownloadResponse(
                status_code=resp.status,
                headers=dict(resp.headers),
                content=body,
                final_url=resp.url,
            )
        except BackendError:
            raise
        except Exception as exc:
            return DownloadResponse(
                status_code=0, headers={}, content=b"", final_url=url,
                transport_error=f"浏览器下载异常: {exc}",
            )

    def close(self) -> None:
        for closer, obj in (
            (lambda: self._page and self._page.close(), None),
            (lambda: self._context and self._context.close(), None),
            (lambda: self._browser and self._browser.close(), None),
            (lambda: self._playwright and self._playwright.stop(), None),
        ):
            try:
                closer()
            except Exception:
                pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
