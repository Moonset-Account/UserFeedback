"""本地公开源站：静态文件 + Range/ETag + 采集变化模拟端点。

- 所有响应都带 ``Accept-Ranges: bytes`` 与基于内容的 ETag；
- 仅处理 GET/HEAD，GET 携带 Range 时返回 206 与 Content-Range；
- ``/packages/mutation.zip`` 是特殊端点：每次 GET 在 mutation_a.zip 与
  mutation_b.zip 之间轮换，用于验证"采集中源包变化"检测；
- HEAD 不触发轮换，保证客户端先拿到稳定元数据。
"""

from __future__ import annotations

import functools
import hashlib
import http.server
import os
import socketserver
import threading
from typing import Optional

CONTENT_TYPES = {
    ".zip": "application/zip",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    # 由 ThreadingHTTPServer 注入
    serve_root: str = ""
    mutation_lock = threading.Lock()
    mutation_toggle = {"on": False, "counter": 0}

    def log_message(self, fmt, *args):  # 静音默认访问日志
        return

    # ------------------------------------------------------------------
    def _resolve_path(self) -> Optional[str]:
        path = self.path.split("?", 1)[0]
        # 仅允许扁平静态访问，拒绝任何父目录跳转
        rel = path.lstrip("/")
        if not rel:
            rel = "index.json"
        target = os.path.normpath(os.path.join(self.serve_root, rel))
        if not target.startswith(os.path.abspath(self.serve_root)):
            return None
        return target

    def _resolve_mutation_file(self) -> Optional[str]:
        """解析 mutation.zip 当前应指向的实体文件（GET 时轮换）。"""
        with self.mutation_lock:
            self.mutation_toggle["counter"] += 1
            use_b = self.mutation_toggle["counter"] % 2 == 0
        name = "mutation_b.zip" if use_b else "mutation_a.zip"
        target = os.path.join(self.serve_root, "packages", name)
        return target if os.path.exists(target) else None

    # ------------------------------------------------------------------
    def do_HEAD(self):
        self._serve(head_only=True)

    def do_GET(self):
        self._serve(head_only=False)

    def _serve(self, head_only: bool) -> None:
        path = self.path.split("?", 1)[0]
        is_mutation = path == "/packages/mutation.zip"
        if is_mutation:
            if head_only:
                # HEAD 不轮换：固定指向 a，保证元数据探测稳定
                target = os.path.join(self.serve_root, "packages", "mutation_a.zip")
            else:
                target = self._resolve_mutation_file()
        else:
            target = self._resolve_path()
        if target is None or not os.path.isfile(target):
            self.send_error(404, "Not Found")
            return

        with open(target, "rb") as fh:
            data = fh.read()
        etag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
        ext = os.path.splitext(target)[1].lower()
        content_type = CONTENT_TYPES.get(ext, "application/octet-stream")

        range_header = self.headers.get("Range")
        if range_header is None or not range_header.startswith("bytes="):
            # 整包请求（如浏览器直接点开）——采集器不会走这条路径
            self.send_response(200)
            self._common_headers(content_type, len(data), etag)
            if not head_only:
                self.wfile.write(data)
            return

        try:
            start, end = self._parse_range(range_header, len(data))
        except ValueError:
            self.send_error(416, "Requested Range Not Satisfiable")
            return

        chunk = data[start : end + 1]
        self.send_response(206)
        self._common_headers(content_type, len(chunk), etag,
                             content_range=f"bytes {start}-{end}/{len(data)}")
        if not head_only:
            self.wfile.write(chunk)

    def _common_headers(self, content_type: str, length: int, etag: str,
                        content_range: Optional[str] = None) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-store")
        if content_range is not None:
            self.send_header("Content-Range", content_range)
        # 所有头必须在 end_headers 之前发送，否则会被静默丢弃
        self.end_headers()

    @staticmethod
    def _parse_range(header: str, total: int) -> tuple[int, int]:
        spec = header.removeprefix("bytes=").strip()
        if "," in spec:
            raise ValueError("本项目源站不支持多区间 Range")
        start_s, end_s = spec.split("-", 1)
        if start_s == "":
            # 后缀区间 bytes=-N
            suffix = int(end_s)
            if suffix <= 0:
                raise ValueError
            return max(0, total - suffix), total - 1
        start = int(start_s)
        end = int(end_s) if end_s else total - 1
        if start < 0 or start >= total or end < start:
            raise ValueError
        return start, min(end, total - 1)


class ThreadingSourceServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, serve_root: str):
        handler = functools.partial(_RangeHandler)
        # 通过类属性注入 serve_root（同一进程仅一个源站实例）
        _RangeHandler.serve_root = os.path.abspath(serve_root)
        _RangeHandler.mutation_toggle = {"on": False, "counter": 0}
        super().__init__(addr, _RangeHandler)


def start_source_server(serve_root: str, host: str = "127.0.0.1", port: int = 0) -> tuple[ThreadingSourceServer, threading.Thread, str]:
    """启动源站，返回 (server, thread, base_url)。"""
    server = ThreadingSourceServer((host, port), serve_root)
    thread = threading.Thread(target=server.serve_forever, name="source-site", daemon=True)
    thread.start()
    actual_host, actual_port = server.server_address
    base_url = f"http://{actual_host}:{actual_port}"
    return server, thread, base_url
