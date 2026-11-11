"""订阅源抓取与 enclosure 可用性探测。

仅通过规定的公开接口交互：
- http(s) URL：标准库 urllib，无自定义鉴权、无外部业务服务；
- file:// URL 或本地路径：只读打开本地文件。

可用性判定（只探测、不下载完整音频、不播放）：
1. 先发 HTTP HEAD；
2. 服务器不允许 HEAD（405/501）时退回带 Range: bytes=0-0 的 GET；
3. 2xx 视为可用；3xx 由重定向处理器记录跳转链后按最终状态判定；
   4xx/5xx、超时、DNS 失败、连接拒绝均判不可用并保留具体错误。
"""
from __future__ import annotations

import http.client
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .errors import FetchError

DEFAULT_TIMEOUT = 5.0
USER_AGENT = "FeedIdentityCollector/1.0 (+local research catalog; no player)"


class _RedirectRecorder(urllib.request.HTTPRedirectHandler):
    def __init__(self) -> None:
        self.chain: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        self.chain.append(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(recorder: _RedirectRecorder) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(recorder)


def is_local_source(source: str) -> bool:
    parsed = urlparse(source)
    return parsed.scheme in ("", "file")


def _read_local(source: str) -> bytes:
    parsed = urlparse(source)
    path = Path(parsed.path if parsed.scheme == "file" else source)
    if not path.is_file():
        raise FetchError(f"本地文件不存在：{path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise FetchError(f"读取本地文件失败：{exc}") from exc


def fetch_feed(source: str, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """抓取订阅源文档字节。"""
    if is_local_source(source):
        return _read_local(source)
    req = urllib.request.Request(
        source, headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml, */*"}
    )
    recorder = _RedirectRecorder()
    try:
        with _opener(recorder).open(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise FetchError(
            f"订阅源返回 HTTP {exc.code} {exc.reason}"
        ) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError,
            ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout) or isinstance(exc, (socket.timeout, TimeoutError)):
            raise FetchError(f"抓取订阅源超时（>{timeout}s）：{source}") from exc
        raise FetchError(f"抓取订阅源失败：{reason}") from exc


@dataclass
class ProbeResult:
    available: bool
    http_status: int | None
    content_type: str | None
    content_length: int | None
    redirect_chain: list[str]
    elapsed_ms: int
    method: str
    error: str | None


def _probe_http(url: str, timeout: float, method: str,
                recorder: _RedirectRecorder) -> ProbeResult:
    import time

    headers = {"User-Agent": USER_AGENT}
    if method == "GET":
        headers["Range"] = "bytes=0-0"
    req = urllib.request.Request(url, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with _opener(recorder).open(req, timeout=timeout) as resp:
            resp.read(64)  # 只读极少字节确认实体可读
            elapsed = int((time.perf_counter() - started) * 1000)
            length = resp.headers.get("Content-Length")
            return ProbeResult(
                available=True,
                http_status=resp.status,
                content_type=resp.headers.get("Content-Type"),
                content_length=int(length) if length and length.isdigit() else None,
                redirect_chain=list(recorder.chain),
                elapsed_ms=elapsed,
                method=method,
                error=None,
            )
    except urllib.error.HTTPError as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        return ProbeResult(
            available=False, http_status=exc.code,
            content_type=exc.headers.get("Content-Type") if exc.headers else None,
            content_length=None,
            redirect_chain=list(recorder.chain),
            elapsed_ms=elapsed, method=method,
            error=f"HTTP {exc.code} {exc.reason}",
        )
    except (urllib.error.URLError, socket.timeout, TimeoutError,
            http.client.HTTPException, OSError) as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout) or isinstance(exc, (socket.timeout, TimeoutError)):
            msg = f"探测超时（>{timeout}s）"
        else:
            msg = f"连接失败：{reason}"
        return ProbeResult(
            available=False, http_status=None, content_type=None,
            content_length=None, redirect_chain=list(recorder.chain),
            elapsed_ms=elapsed, method=method, error=msg,
        )


def probe_url(url: str, timeout: float = DEFAULT_TIMEOUT) -> ProbeResult:
    """探测附件/封面地址的可用性，不下载完整文件。"""
    parsed = urlparse(url)
    if parsed.scheme in ("", "file"):
        path = Path(parsed.path if parsed.scheme == "file" else url)
        if path.is_file():
            return ProbeResult(
                available=True, http_status=None,
                content_type=None, content_length=path.stat().st_size,
                redirect_chain=[], elapsed_ms=0, method="FILE", error=None,
            )
        return ProbeResult(
            available=False, http_status=None, content_type=None,
            content_length=None, redirect_chain=[], elapsed_ms=0,
            method="FILE", error=f"本地文件不存在：{path}",
        )

    recorder = _RedirectRecorder()
    result = _probe_http(url, timeout, "HEAD", recorder)
    if result.http_status in (405, 501):
        # 服务器不支持 HEAD，退回 Range GET
        result = _probe_http(url, timeout, "GET", _RedirectRecorder())
    return result
