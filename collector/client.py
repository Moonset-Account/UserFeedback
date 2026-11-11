"""采集器 HTTP 客户端：只发出 RequestBuilder 构造好的只读 GET 请求。"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

import requests

from .builder import BuiltRequest, RequestBuilder
from .loader import OpenAPISpec


@dataclass
class RunResult:
    request: BuiltRequest
    status_code: int
    response_json: dict | None
    response_text: str
    response_headers: dict
    final_url: str
    error: str | None = None
    elapsed_ms: int = 0
    data: list | dict | None = field(default=None, repr=False)
    echo: dict | None = field(default=None, repr=False)


class CollectorClient:
    """对源站发起真实采集；base_url 仅允许 http/https 的本地/相对地址。"""

    def __init__(self, spec: OpenAPISpec, base_url: str, timeout: float = 10.0) -> None:
        self.spec = spec
        self.builder = RequestBuilder(spec)
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        scheme = urlsplit(self.base_url).scheme
        if scheme not in ("http", "https"):
            raise ValueError(f"base_url 必须是 http(s) 地址，收到 {base_url!r}")

    def preview(self, operation_id: str, params: dict | None = None) -> BuiltRequest:
        """只构造不发送，供页面展示“生成的请求”。"""
        return self.builder.build(operation_id, params)

    def run(self, operation_id: str, params: dict | None = None) -> RunResult:
        """构造并真实发送 GET 请求；构造失败时不会发出任何网络请求。"""
        built = self.builder.build(operation_id, params)
        url = self.base_url + built.url
        headers = {"Accept": "application/json", "User-Agent": "rookie-collector/1.0"}
        try:
            resp = requests.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            return RunResult(
                request=built, status_code=0, response_json=None,
                response_text="", response_headers={}, final_url=url,
                error=f"网络请求失败: {exc}",
            )

        text = resp.text
        try:
            body = resp.json()
        except ValueError:
            body = None
        return RunResult(
            request=built,
            status_code=resp.status_code,
            response_json=body,
            response_text=text,
            response_headers=dict(resp.headers),
            final_url=resp.url,
            elapsed_ms=int(resp.elapsed.total_seconds() * 1000),
            data=(body or {}).get("data") if isinstance(body, dict) else None,
            echo=(body or {}).get("echo") if isinstance(body, dict) else None,
        )
