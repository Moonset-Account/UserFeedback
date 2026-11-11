"""Web 应用：中文页面 + JSON API（纯标准库实现）。

路由：
- GET  /                 中文操作页面
- GET  /static/*         静态资源
- GET  /api/meta         源站地址、实验 ID、支持的协议子集、场景清单
- POST /api/tasks        创建采集任务
- GET  /api/tasks        任务列表
- GET  /api/tasks/<id>   任务详情（含 Range 证据、记录、错误）
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
from typing import Any

from sampler.experiment_data import EXPERIMENT_ID
from selective_zip.protocol import SUPPORTED_SUBSET_DESCRIPTION

from .tasks import TaskManager

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_INDEX_HTML = os.path.join(_STATIC_DIR, "index.html")

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class WebHandler(http.server.BaseHTTPRequestHandler):
    # 由 server 注入
    source_base_url: str = ""
    data_dir: str = ""
    task_manager: TaskManager = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):
        return

    # ------------------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send_file(_INDEX_HTML, "text/html; charset=utf-8")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/") :])
            return
        if path == "/api/meta":
            self._send_json(self._build_meta())
            return
        if path == "/api/tasks":
            self._send_json({"tasks": self.task_manager.list_tasks()})
            return
        if path.startswith("/api/tasks/"):
            task_id = path.rsplit("/", 1)[-1]
            task = self.task_manager.get_task(task_id)
            if task is None:
                self._send_json({"error": "任务不存在"}, status=404)
            else:
                self._send_json(task.to_dict())
            return
        self._send_json({"error": "Not Found"}, status=404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/tasks":
            self._send_json({"error": "Not Found"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._send_json({"error": f"请求体不是合法 JSON：{exc}"}, status=400)
            return

        url = payload.get("url")
        members = payload.get("wanted_members")
        if not isinstance(url, str) or not url:
            self._send_json({"error": "缺少 url 参数"}, status=400)
            return
        if not isinstance(members, list) or not members or not all(isinstance(m, str) for m in members):
            self._send_json({"error": "wanted_members 必须是非空字符串数组"}, status=400)
            return
        params = {
            "url": url,
            "wanted_members": members,
            "experiment_id": payload.get("experiment_id") or EXPERIMENT_ID,
            "allowed_versions": payload.get("allowed_versions", (1, 2, 3)),
            "timeout": payload.get("timeout", 30.0),
            "label": payload.get("label", ""),
        }
        task = self.task_manager.create_task(params)
        self._send_json(task.to_dict(), status=201)

    # ------------------------------------------------------------------
    def _build_meta(self) -> dict[str, Any]:
        index_path = os.path.join(self.data_dir, "index.json")
        with open(index_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        return {
            "source_base_url": self.source_base_url,
            "experiment_id": EXPERIMENT_ID,
            "protocol_subset": SUPPORTED_SUBSET_DESCRIPTION,
            "scenarios": manifest["scenarios"],
            "packages": manifest["packages"],
            "mutation_endpoint": manifest["mutation_endpoint"],
        }

    def _serve_static(self, rel: str) -> None:
        target = os.path.normpath(os.path.join(_STATIC_DIR, rel))
        if not target.startswith(_STATIC_DIR) or not os.path.isfile(target):
            self._send_json({"error": "Not Found"}, status=404)
            return
        ext = os.path.splitext(target)[1].lower()
        self._send_file(target, STATIC_TYPES.get(ext, "application/octet-stream"))

    def _send_file(self, path: str, content_type: str) -> None:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            self._send_json({"error": "Not Found"}, status=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj: Any, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


class ThreadingWebServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_web_server(host: str, port: int, source_base_url: str, data_dir: str) -> ThreadingWebServer:
    manager = TaskManager()
    handler = WebHandler
    handler.source_base_url = source_base_url
    handler.data_dir = data_dir
    handler.task_manager = manager
    return ThreadingWebServer((host, port), handler)
