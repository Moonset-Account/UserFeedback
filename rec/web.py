"""统一 Web 服务：中文操作页面、任务 API、演示编排与源站挂载。

一个端口同时提供：
- ``/``                操作页面（HTML/CSS/JS 静态文件）
- ``/api/*``           任务管理与演示 API
- ``/origin/*``        本地 HTTP 源站（采集器唯一数据入口）
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from rec.engine import Engine, TERMINAL_STATUSES
from rec.origin import OriginMethods, OriginServer
from rec import generator

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
STATIC_DIR = os.path.join(PROJECT_DIR, "static")

# 页面可选的完整验收场景目录
SCENARIO_CATALOG: List[Dict[str, Any]] = [
    {"key": "benign", "name": "正常分段采集",
     "desc": "标准 206 响应，演示 If-Range 续取与区间合并。",
     "object": "large-v1.expb", "replacement": None},
    {"key": "drop-first", "name": "传输中断后续取",
     "desc": "首个分段在传输中途断开，随后正常 206 续取。",
     "object": "large-v1.expb", "replacement": None},
    {"key": "swap", "name": "续取时源对象被替换",
     "desc": "第二次请求时对象换成新版本，ETag 变化，旧版本字节被拒绝。",
     "object": "large-v1.expb", "replacement": "large-v2.expb"},
    {"key": "error-region", "name": "服务器错误区间",
     "desc": "先返回两次 500，再返回 416，退避重试后恢复 206。",
     "object": "large-v1.expb", "replacement": None},
    {"key": "ignore-range", "name": "源站忽略 Range 返回 200",
     "desc": "前两次忽略 Range 返回全量 200，采集器整体校验后接受。",
     "object": "large-v1.expb", "replacement": None},
    {"key": "bad-content-range", "name": "不一致的 Content-Range",
     "desc": "字节存在但 Content-Range 总长度字段错误，响应必须被拒绝。",
     "object": "large-v1.expb", "replacement": None},
    {"key": "bad-length", "name": "Content-Length 与实际字节不符",
     "desc": "声明长度大于实际送出字节并断连，截断响应必须被拒绝。",
     "object": "large-v1.expb", "replacement": None},
]

DEMO_STEPS = [
    {"key": "drop-first", "name": "正常续取（中断恢复）",
     "object": "tiny-v1.expb", "replacement": None, "chunk_size": 1024},
    {"key": "swap", "name": "边界场景（续取时对象变更）",
     "object": "tiny-v1.expb", "replacement": "tiny-v2.expb",
     "chunk_size": 1024},
]


class TaskManager:
    def __init__(self, data_dir: str, origin: OriginServer,
                 base_url: str) -> None:
        self.data_dir = data_dir
        self.tasks_dir = os.path.join(data_dir, "tasks")
        os.makedirs(self.tasks_dir, exist_ok=True)
        self.origin = origin
        self.base_url = base_url.rstrip("/")
        self.engines: Dict[str, Engine] = {}
        self.threads: Dict[str, threading.Thread] = {}
        self.meta: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()
        self._recover()

    def _recover(self) -> None:
        """进程重启后扫描任务目录，状态可继续但不自动运行。"""
        if not os.path.isdir(self.tasks_dir):
            return
        for task_id in sorted(os.listdir(self.tasks_dir)):
            state_path = os.path.join(self.tasks_dir, task_id, "state.json")
            if not os.path.exists(state_path):
                continue
            with open(state_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
            self.meta[task_id] = {
                "label": state.get("scenario", task_id),
                "created_at": state.get("started_at") or state.get("updated_at"),
                "recovered": True,
            }

    def list_tasks(self) -> List[Dict[str, Any]]:
        # 按创建时间倒序（task_id 含时间戳），最新任务排在首位
        ordered = sorted(self.meta, reverse=True)
        out = [self.describe(task_id) for task_id in ordered]
        return [item for item in out if item]

    def describe(self, task_id: str) -> Optional[Dict[str, Any]]:
        engine = self.engines.get(task_id)
        task_dir = os.path.join(self.tasks_dir, task_id)
        state_path = os.path.join(task_dir, "state.json")
        if engine is None and not os.path.exists(state_path):
            return None
        if engine is None:
            with open(state_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
            from rec import intervals
            length = state.get("expected_length")
            spans = [tuple(s) for s in state.get("spans", [])]
            state["gaps"] = intervals.gaps(spans, length) if length else []
            state["covered"] = (
                intervals.covered_count(spans, length) if length else 0
            )
            state["object_path"] = os.path.join(task_dir, "object.bin")
            state["task_id"] = task_id
            state["recovered"] = self.meta.get(task_id, {}).get("recovered", False)
            state["label"] = self.meta.get(task_id, {}).get("label", task_id)
            return state
        snap = engine.snapshot()
        snap["task_id"] = task_id
        snap["recovered"] = self.meta.get(task_id, {}).get("recovered", False)
        snap["label"] = self.meta.get(task_id, {}).get("label", task_id)
        return snap

    def create_task(self, scenario_key: str, object_name: str,
                    replacement: Optional[str], chunk_size: int,
                    label: str) -> Dict[str, Any]:
        task_id = time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
        token = "t" + uuid.uuid4().hex[:12]
        self.origin.configure(token, object_name, scenario_key, replacement)
        url = f"{self.base_url}/origin/s/{token}/{object_name}"
        task_dir = os.path.join(self.tasks_dir, task_id)
        engine = Engine(task_dir, url, label, chunk_size=chunk_size)
        with self.lock:
            self.engines[task_id] = engine
            self.meta[task_id] = {
                "label": label,
                "created_at": time.time(),
                "recovered": False,
                "scenario_key": scenario_key,
                "object": object_name,
                "replacement": replacement,
                "token": token,
            }
        return {"task_id": task_id, "url": url}

    def start(self, task_id: str) -> bool:
        with self.lock:
            engine = self.engines.get(task_id)
            thread = self.threads.get(task_id)
            if engine is None:
                # 恢复任务：重新装配引擎（状态从磁盘读取）
                task_dir = os.path.join(self.tasks_dir, task_id)
                state_path = os.path.join(task_dir, "state.json")
                if not os.path.exists(state_path):
                    return False
                with open(state_path, "r", encoding="utf-8") as fh:
                    state = json.load(fh)
                if state["status"] in TERMINAL_STATUSES:
                    return False
                engine = Engine(task_dir, state["url"],
                                state.get("scenario", task_id),
                                chunk_size=state.get("chunk_size", 2048))
                self.engines[task_id] = engine
                self.meta.setdefault(task_id, {})["recovered"] = True
            if thread and thread.is_alive():
                return True
            runner = threading.Thread(
                target=engine.run, name=f"task-{task_id}", daemon=True,
            )
            self.threads[task_id] = runner
            runner.start()
        return True

    def pause(self, task_id: str) -> bool:
        engine = self.engines.get(task_id)
        if engine is None:
            return False
        engine.pause()
        return True

    def stop(self, task_id: str) -> bool:
        engine = self.engines.get(task_id)
        if engine is None:
            return False
        engine.stop()
        return True

    def events(self, task_id: str, limit: int = 200) -> Optional[List[Dict[str, Any]]]:
        engine = self.engines.get(task_id)
        events_path = os.path.join(self.tasks_dir, task_id, "events.jsonl")
        if engine is not None:
            return engine.read_events(limit)
        if not os.path.exists(events_path):
            return None
        with open(events_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()[-limit:]
        return [json.loads(line) for line in lines if line.strip()]

    def wait_for(self, task_id: str, timeout: float = 12.0,
                 poll: float = 0.05) -> Optional[str]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            snap = self.describe(task_id)
            if snap and snap["status"] in TERMINAL_STATUSES:
                return snap["status"]
            time.sleep(poll)
        return None

    def run_demo(self) -> Dict[str, Any]:
        """依次运行正常续取与边界场景（小对象，目标 15 秒内结束）。"""
        created = []
        for step in DEMO_STEPS:
            info = self.create_task(
                step["key"], step["object"], step["replacement"],
                step["chunk_size"], step["name"],
            )
            task_id = info["task_id"]
            self.start(task_id)
            created.append({"task_id": task_id, **step, "url": info["url"]})
        return {"tasks": created}

    def objects(self) -> List[Dict[str, Any]]:
        objects_dir = os.path.join(self.data_dir, "objects")
        result = []
        if os.path.isdir(objects_dir):
            for name in sorted(os.listdir(objects_dir)):
                if name.endswith(".expb"):
                    result.append({
                        "name": name,
                        "size": os.path.getsize(os.path.join(objects_dir, name)),
                    })
        return result


class AppHandler(OriginMethods, BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    manager: TaskManager = None  # type: ignore[assignment]
    origin: OriginServer = None  # type: ignore[assignment]
    data_dir: str = ""

    def log_message(self, *_args) -> None:
        return

    # ------------------------------------------------------------ 路由入口
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.startswith("/origin/"):
            self.handle_origin_get()
        elif path.startswith("/api/"):
            self._api_get(path)
        else:
            self._static(path)

    def do_HEAD(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0].startswith("/origin/"):
            self.handle_origin_head()
        else:
            self.send_error(405)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.startswith("/origin/"):
            self.send_error(405)
            return
        if path.startswith("/api/"):
            self._api_post(path)
            return
        self.send_error(404)
    # --------------------------------------------------------------- 静态
    def _static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            path = "/index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR + os.sep) and full != STATIC_DIR:
            self.send_error(403)
            return
        if not os.path.isfile(full):
            self.send_error(404)
            return
        suffix = os.path.splitext(full)[1]
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }.get(suffix, "application/octet-stream")
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ----------------------------------------------------------------- API
    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _api_get(self, path: str) -> None:
        manager = self.manager
        if path == "/api/scenarios":
            self._json({"scenarios": SCENARIO_CATALOG, "demo_steps": DEMO_STEPS})
            return
        if path == "/api/objects":
            self._json({"objects": manager.objects()})
            return
        if path == "/api/tasks":
            self._json({"tasks": manager.list_tasks()})
            return
        if path.startswith("/api/tasks/"):
            rest = path[len("/api/tasks/"):]
            parts = rest.strip("/").split("/")
            if len(parts) == 1:
                snap = manager.describe(parts[0])
                self._json(snap if snap else {"error": "任务不存在"},
                           200 if snap else 404)
                return
            if len(parts) == 2 and parts[1] == "events":
                events = manager.events(parts[0])
                if events is None:
                    self._json({"error": "任务不存在"}, 404)
                else:
                    self._json({"events": events})
                return
        self.send_error(404)

    def _api_post(self, path: str) -> None:
        manager = self.manager
        if path == "/api/tasks":
            body = self._read_body()
            scenario_key = body.get("scenario", "benign")
            catalog = {item["key"]: item for item in SCENARIO_CATALOG}
            if scenario_key not in catalog:
                self._json({"error": f"未知场景 {scenario_key}"}, 400)
                return
            item = catalog[scenario_key]
            object_name = body.get("object") or item["object"]
            replacement = body.get("replacement", item["replacement"])
            chunk_size = int(body.get("chunk_size") or 2048)
            label = body.get("label") or item["name"]
            info = manager.create_task(
                scenario_key, object_name, replacement, chunk_size, label,
            )
            manager.start(info["task_id"])
            self._json(info, 201)
            return
        if path == "/api/demo":
            self._json(manager.run_demo(), 201)
            return
        if path.startswith("/api/tasks/"):
            rest = path[len("/api/tasks/"):]
            parts = rest.strip("/").split("/")
            if len(parts) != 2:
                self.send_error(404)
                return
            task_id, action = parts
            if action == "start":
                ok = manager.start(task_id)
            elif action == "pause":
                ok = manager.pause(task_id)
            elif action == "stop":
                ok = manager.stop(task_id)
            else:
                self.send_error(404)
                return
            self._json({"ok": ok}, 200 if ok else 409)
            return
        self.send_error(404)


def build_app(host: str, port: int, data_dir: str) -> ThreadingHTTPServer:
    objects_dir = os.path.join(data_dir, "objects")
    if not os.path.isdir(objects_dir):
        generator.generate_all(data_dir)

    origin = OriginServer()
    origin.load_objects(objects_dir)
    base_url = f"http://{host}:{port}"
    manager = TaskManager(data_dir, origin, base_url)

    handler = type("BoundAppHandler", (AppHandler,), {
        "manager": manager,
        "origin": origin,
        "data_dir": data_dir,
    })
    server = ThreadingHTTPServer((host, port), handler)
    server.manager = manager  # type: ignore[attr-defined]
    server.origin = origin  # type: ignore[attr-defined]
    return server
