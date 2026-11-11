"""最小 HTTP/1.1 客户端封装。

支持的协议子集（与 README 声明一致）：
- 仅 ``http`` 方案，且主机限定为回环地址（127.0.0.1 / localhost / ::1）；
- GET/HEAD，请求头 ``Range``、``If-Range``、``Accept-Encoding: identity``；
- 响应码 200/206/416/5xx，逐跳/内容头 ``Content-Length``、``Content-Range``、
  ``ETag``、``Accept-Ranges``、``Last-Modified``；
- 不跟随重定向、不使用压缩、不支持代理与 keep-alive 复用（每次新建连接）。

连接在读完声明长度前中断时不抛异常，而是返回 ``truncated=True`` 并保留
已收到的字节，供引擎记录证据后丢弃。
"""

from __future__ import annotations

import http.client
from dataclasses import dataclass, field
from typing import Dict, Optional
from urllib.parse import urlparse

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_TIMEOUT = 10.0


class HttpProtocolError(RuntimeError):
    """超出声明协议子集的用法（如非 http 方案、非回环主机、重定向等）。"""


class TransportError(RuntimeError):
    """连接建立/发送阶段失败，没有任何可用响应。"""


@dataclass
class HttpResponse:
    status: int
    headers: Dict[str, str]
    body: bytes
    truncated: bool = False
    note: str = ""
    url: str = ""
    request_headers: Dict[str, str] = field(default_factory=dict)


def _split_url(url: str) -> tuple[str, int, str]:
    parsed = urlparse(url)
    if parsed.scheme != "http":
        raise HttpProtocolError(f"仅支持 http 方案，收到 {parsed.scheme!r}")
    host = parsed.hostname or ""
    if host not in ALLOWED_HOSTS:
        raise HttpProtocolError(f"仅允许访问回环源站，收到主机 {host!r}")
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return host, port, path


def _request(
    url: str,
    method: str,
    extra_headers: Optional[Dict[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> HttpResponse:
    host, port, path = _split_url(url)
    headers = {"Accept-Encoding": "identity", "Connection": "close"}
    if extra_headers:
        headers.update(extra_headers)

    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request(method, path, headers=headers)
        resp = conn.getresponse()
    except (OSError, http.client.HTTPException) as exc:
        raise TransportError(f"{method} {url} 连接失败: {exc}") from exc

    if resp.status in (301, 302, 303, 307, 308):
        conn.close()
        raise HttpProtocolError("协议子集不包含重定向")

    try:
        body = resp.read()
        truncated = False
        note = ""
    except http.client.IncompleteRead as exc:
        # 源站在传输中途断开：保留部分字节作为证据
        body = exc.partial or b""
        truncated = True
        note = f"连接在读完声明长度前中断（IncompleteRead，已收到 {len(body)} 字节）"
    finally:
        conn.close()

    lower_headers = {k.lower(): v for k, v in resp.getheaders()}
    return HttpResponse(
        status=resp.status,
        headers=lower_headers,
        body=body,
        truncated=truncated,
        note=note,
        url=url,
        request_headers=dict(headers),
    )


def head(url: str, timeout: float = DEFAULT_TIMEOUT) -> HttpResponse:
    return _request(url, "HEAD", timeout=timeout)


def get_range(
    url: str,
    start: int,
    end: Optional[int],
    if_range: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> HttpResponse:
    """请求 [start, end] 闭区间；end 为 None 时使用开放式 ``bytes=start-``。"""
    if end is None:
        range_value = f"bytes={start}-"
    else:
        range_value = f"bytes={start}-{end}"
    extra = {"Range": range_value}
    if if_range:
        extra["If-Range"] = if_range
    return _request(url, "GET", extra, timeout=timeout)
