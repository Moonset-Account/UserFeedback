"""三种采集模式的具体实现。"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from classifier.models import FetchResult

USER_AGENT = "SemanticJudgeCollector/1.0 (local-acceptance; fictional-data-only)"
DEFAULT_TIMEOUT = 8.0


class FetchError(RuntimeError):
    """传输层失败（连接、超时、截断等）。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _extract_nav_source(html: str, fallback: str = "") -> str:
    soup = BeautifulSoup(html[:200_000], "html.parser")
    meta = soup.find("meta", attrs={"name": "x-nav-source"})
    if meta and meta.get("content"):
        return meta["content"].strip()
    return fallback


def fetch_with_http(
    url: str,
    *,
    referer: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> FetchResult:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    if referer:
        headers["Referer"] = referer
    started = time.monotonic()
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, stream=True)
        # stream=True 让内容截断类故障在 read 阶段显式暴露
        chunks: list[bytes] = []
        for chunk in resp.iter_content(chunk_size=8192):
            chunks.append(chunk)
        raw = b"".join(chunks)
        resp.close()
    except requests.RequestException as exc:
        raise FetchError(f"HTTP 采集失败：{type(exc).__name__}: {exc}") from exc

    elapsed = int((time.monotonic() - started) * 1000)
    encoding = resp.encoding or "utf-8"
    try:
        html = raw.decode(encoding, errors="replace")
    except LookupError:
        html = raw.decode("utf-8", errors="replace")
    return FetchResult(
        ok=True,
        url=url,
        status_code=resp.status_code,
        content_type=resp.headers.get("Content-Type", ""),
        html=html,
        fetched_at=_now_iso(),
        elapsed_ms=elapsed,
        fetch_mode="http",
        referer=referer,
        nav_source=_extract_nav_source(html),
    )


def fetch_with_file(path: str | Path, *, base_url_hint: str = "") -> FetchResult:
    p = Path(path)
    started = time.monotonic()
    if not p.is_file():
        raise FetchError(f"文件采集失败：不存在或不是文件：{p}")
    try:
        html = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise FetchError(f"文件采集失败：{type(exc).__name__}: {exc}") from exc
    elapsed = int((time.monotonic() - started) * 1000)
    url = base_url_hint or p.resolve().as_uri()
    return FetchResult(
        ok=True,
        url=url,
        status_code=200,
        content_type="text/html; charset=utf-8",
        html=html,
        fetched_at=_now_iso(),
        elapsed_ms=elapsed,
        fetch_mode="file",
        nav_source=_extract_nav_source(html),
    )


def fetch_with_browser(
    url: str,
    *,
    referer: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    wait_until: str = "domcontentloaded",
) -> FetchResult:
    """Playwright 无头浏览器采集。仅在安装 requirements-browser.txt 后可用。

    注意：浏览器会自动补全未闭合标签，渲染后 DOM 会掩盖模板残缺信号，
    因此判定用 HTML 取 response.body() 的线路原始字节；
    渲染后 DOM 仅作为 rendered_html 附在结果里供人工对照。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchError("浏览器模式目标必须是 http(s) URL")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise FetchError(
            "浏览器模式不可用：未安装 Playwright。"
            "请执行 pip install -r requirements-browser.txt 并运行 "
            "python -m playwright install chromium"
        ) from exc

    started = time.monotonic()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=USER_AGENT,
                extra_http_headers={"Referer": referer} if referer else None,
            )
            try:
                response = page.goto(url, wait_until=wait_until, timeout=int(timeout * 1000))
                if response is None:
                    raise FetchError("浏览器未收到任何响应")
                # 线路原始响应体：保留截断/未闭合等结构事实
                try:
                    raw_body = response.body()
                except Exception:
                    raw_body = b""
                rendered_html = page.content()
                status = response.status
                ctype = response.headers.get("content-type", "")
            finally:
                browser.close()
    except FetchError:
        raise
    except Exception as exc:  # playwright 的异常族较宽，统一转为传输失败
        raise FetchError(f"浏览器采集失败：{type(exc).__name__}: {exc}") from exc

    elapsed = int((time.monotonic() - started) * 1000)
    html = raw_body.decode("utf-8", errors="replace") if raw_body else rendered_html
    result = FetchResult(
        ok=True,
        url=url,
        status_code=status,
        content_type=ctype,
        html=html,
        fetched_at=_now_iso(),
        elapsed_ms=elapsed,
        fetch_mode="browser",
        referer=referer,
        nav_source=_extract_nav_source(html),
        rendered_html=rendered_html,
    )
    return result


def fetch(target: str, *, mode: str = "http", referer: str = "",
          timeout: float = DEFAULT_TIMEOUT) -> FetchResult:
    """统一入口。target 为 URL 或文件路径；mode ∈ http/file/browser。"""
    if mode == "http":
        return fetch_with_http(target, referer=referer, timeout=timeout)
    if mode == "file":
        return fetch_with_file(target)
    if mode == "browser":
        return fetch_with_browser(target, referer=referer, timeout=timeout)
    raise FetchError(f"未知采集模式：{mode}（支持 http / file / browser）")
