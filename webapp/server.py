"""本地 Web 应用：中文操作入口 + JSON API + 本地公开源站。

路由：
- GET  /                     中文网页（时间轴、配置、证据与错误）
- GET  /static/*             静态资源
- GET  /source/<file>.ics    本地源站：对外公开的虚构 ICS（采集器通过该 HTTP 接口采集）
- GET  /api/presets          样例预设
- POST /api/collect          运行一次采集任务
- POST /api/run-demo         依次运行"正常 + 边界"两个小样例
- GET  /api/runs、/api/runs/<id>、/api/targets
"""

from __future__ import annotations

import json
import mimetypes
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from collector.service import RunRequest, collect
from collector.store import JsonStore
from samples.generator import PRESETS, generate

# webapp/server.py 的上两级才是项目根（无论从哪个工作目录启动都稳定）
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "web"
GENERATED_DIR = PROJECT_ROOT / "generated"
SOURCE_DIR = GENERATED_DIR / "source"
STORE_DIR = GENERATED_DIR / "store"


class AppState:
    def __init__(self):
        self.lock = threading.Lock()
        self.store = JsonStore(STORE_DIR)
        self.last_demo: dict | None = None


STATE = AppState()


def _preset_source_url(preset_key: str) -> str:
    return f"/source/{PRESETS[preset_key]['file']}"


class Handler(BaseHTTPRequestHandler):
    server_version = "ObservationWindowCollector/1.0"

    # --- 基础工具 ------------------------------------------------------- #
    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, rel_path: str) -> None:
        target = (STATIC_DIR / rel_path).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            # send_error 的消息走 latin-1 头，不能放中文
            self.send_error(404, "Resource not found")
            return
        body = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix in (".html", ".js", ".css"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_source(self, filename: str) -> None:
        # 只暴露 generated/source 下的 .ics，防止路径穿越
        safe = pathlib.Path(filename).name
        target = (SOURCE_DIR / safe).resolve()
        if safe != filename or target.suffix != ".ics" or not target.is_file():
            self.send_error(404, "Source file not found")
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/calendar; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 精简控制台日志
        print(f"[web] {self.address_string()} {fmt % args}")

    # --- GET ------------------------------------------------------------ #
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send_static("index.html")
        elif path.startswith("/static/"):
            self._send_static(path[len("/static/"):])
        elif path.startswith("/source/"):
            self._send_source(path[len("/source/"):])
        elif path == "/api/presets":
            self._send_json({"presets": [
                {"key": k, **v, "source_url": _preset_source_url(k)}
                for k, v in PRESETS.items()
            ]})
        elif path == "/api/runs":
            self._send_json({"runs": STATE.store.list_runs()})
        elif path.startswith("/api/runs/"):
            run = STATE.store.load_run(path.rsplit("/", 1)[1])
            self._send_json(run or {"error": "运行记录不存在"}, 200 if run else 404)
        elif path == "/api/targets":
            self._send_json({"targets": STATE.store.list_targets()})
        elif path == "/api/last-demo":
            self._send_json(STATE.last_demo or {"runs": []})
        else:
            self.send_error(404, "Endpoint not found")

    # --- POST ----------------------------------------------------------- #
    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, json.JSONDecodeError):
            self._send_json({"error": "请求体不是合法 JSON"}, 400)
            return

        if path == "/api/collect":
            self._handle_collect(data)
        elif path == "/api/run-demo":
            self._handle_run_demo(data)
        else:
            self.send_error(404, "Endpoint not found")

    def _resolve_source(self, source: str) -> str:
        """网页传入的相对 /source/... 路径解析为本地文件（仍走采集器的 file 接口）。"""
        if source.startswith("/source/"):
            return str((SOURCE_DIR / pathlib.Path(source).name).resolve())
        return source

    def _handle_collect(self, data: dict) -> None:
        try:
            request = RunRequest.from_dict({
                **data,
                "source": self._resolve_source(str(data.get("source", ""))),
            })
        except (KeyError, TypeError, ValueError) as exc:
            self._send_json({"error": f"请求参数错误：{exc}"}, 400)
            return
        with STATE.lock:
            result = collect(request, storage=STATE.store)
        self._send_json(result, 200 if result["status"] != "failed" else 422)

    def _handle_run_demo(self, data: dict) -> None:
        keys = data.get("preset_keys") or ["demo_normal", "demo_boundary"]
        runs = []
        for key in keys:
            if key not in PRESETS:
                self._send_json({"error": f"未知预设：{key}"}, 400)
                return
            preset = PRESETS[key]
            request = RunRequest(
                source=str((SOURCE_DIR / preset["file"]).resolve()),
                range_start=preset["range_start"],
                range_end=preset["range_end"],
                target_name=f"demo_{key}",
            )
            with STATE.lock:
                runs.append(collect(request, storage=STATE.store))
        demo_payload = {"preset_keys": keys, "runs": runs}
        with STATE.lock:
            STATE.last_demo = demo_payload
        self._send_json(demo_payload)


def ensure_samples() -> None:
    """启动时确保样例数据存在（幂等）。"""
    if not SOURCE_DIR.exists() or not list(SOURCE_DIR.glob("*.ics")):
        generate(GENERATED_DIR)


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    ensure_samples()
    return ThreadingHTTPServer((host, port), Handler)
