"""本地虚构源站：把夹具目录通过 HTTP 公开，并提供错误/超时端点。

采集器只能像访问普通网站一样经 HTTP 读取这些资源，
源站不提供任何“采集结果”接口。
"""
from __future__ import annotations

import functools
import http.server
import socketserver
import threading
from pathlib import Path

_CONTENT_TYPES = {
    ".xml": "application/xml; charset=utf-8",
    ".rss": "application/rss+xml; charset=utf-8",
    ".atom": "application/atom+xml; charset=utf-8",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".png": "image/png",
    ".html": "text/html; charset=utf-8",
}


class _SourceHandler(http.server.BaseHTTPRequestHandler):
    source_root: Path = Path(".")
    slow_delay: float = 8.0

    def log_message(self, fmt: str, *args) -> None:  # 静音访问日志
        return

    def handle_one_request(self) -> None:
        # 可用性探测客户端可能提前断开连接，忽略管道破裂噪音
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send(self, status: int, body: bytes, content_type: str,
              extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(send_body=False)

    def do_GET(self) -> None:  # noqa: N802
        self._serve(send_body=True)

    def _serve(self, send_body: bool) -> None:
        path = self.path.split("?", 1)[0]

        # ---- 错误端点（含 405，用于验证 HEAD→GET 回退） ----
        if path == "/error/404.mp3":
            return self._send(404, b"not found (fictional)", "text/plain; charset=utf-8")
        if path == "/error/410.mp3":
            return self._send(410, b"gone (fictional)", "text/plain; charset=utf-8")
        if path == "/error/503.mp3":
            self.send_response(503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Retry-After", "60")
            self.send_header("Content-Length", "22")
            self.end_headers()
            if send_body:
                self.wfile.write(b"service unavailable (fictional)")
            return
        if path == "/error/head-not-allowed.mp3":
            if self.command == "HEAD":
                return self._send(405, b"method not allowed (fictional)",
                                  "text/plain; charset=utf-8",
                                  {"Allow": "GET"})
            return self._send(200, b"\xff\xfb\x90\x04" + b"\x00" * 64, "audio/mpeg")
        if path == "/error/cover-missing.png":
            return self._send(404, b"cover missing (fictional)", "text/plain; charset=utf-8")
        if path == "/error/missing-feed.xml":
            return self._send(404, b"feed missing (fictional)", "text/plain; charset=utf-8")
        if path == "/error/slow.xml":
            # 可控延迟，用于超时测试；测试会用很短的 timeout 触发
            import time
            time.sleep(self.slow_delay)
            return self._send(200, b"<feed/>", "application/xml")

        # ---- 静态夹具（防目录穿越） ----
        relative = path.lstrip("/")
        target = (self.source_root / relative).resolve()
        root = self.source_root.resolve()
        if root not in target.parents and target != root:
            return self._send(403, b"forbidden", "text/plain; charset=utf-8")
        if not target.is_file():
            return self._send(404, b"not found (fictional)", "text/plain; charset=utf-8")
        body = target.read_bytes()
        ctype = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        return self._send(200, body, ctype)


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class LocalSourceServer:
    """在后台线程运行的本地源站。"""

    def __init__(self, source_root: str | Path, host: str = "127.0.0.1",
                 port: int = 0, slow_delay: float = 8.0) -> None:
        handler = type("BoundHandler", (_SourceHandler,),
                       {"source_root": Path(source_root),
                        "slow_delay": slow_delay})
        self.httpd = _ThreadingServer((host, port), handler)
        self.host, self.port = self.httpd.server_address[:2]
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, name="sample-source", daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def __enter__(self) -> "LocalSourceServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)


def build_server(source_root: str | Path, **kwargs) -> LocalSourceServer:
    return LocalSourceServer(source_root, **kwargs)
