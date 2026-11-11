"""本地源站 + 中文操作控制台（Flask）。

路由
====
* ``GET  /``                中文控制台（配置输入、运行任务、证据与错误查看）
* ``GET  /editor``          受控曲线编辑器页面（RPA 操作对象）
* ``GET  /api/tasks``       公开接口：任务清单（HTTP/1.1 + JSON 子集）
* ``GET  /api/tasks/<id>``  公开接口：完整任务规格（含目标点，供采集器）
* ``GET  /api/editor/<id>`` 公开接口：编辑器视图（不含目标点，防泄题）
* ``POST /api/run``         控制台触发一次真实 RPA 执行
* ``GET  /api/jobs/<id>``   查询执行结果与证据
"""
from __future__ import annotations

import argparse
import threading
import traceback
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from .engine import CurveEditorAutomation
from .sample_gen import TASKS_DIR, TRUTH_DIR, write_samples
from .task_schema import editor_view, parse_task

ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = ROOT / "static"

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _load_task_raw(task_id: str) -> dict[str, Any]:
    import json

    path = TASKS_DIR / f"{task_id}.json"
    if not path.exists():
        raise FileNotFoundError(task_id)
    return json.loads(path.read_text(encoding="utf-8"))


def create_app() -> Flask:
    # 源站启动即确保样例由生成器产出（采集器与生成器职责仍分属不同模块）
    if not (TASKS_DIR / "index.json").exists():
        write_samples()

    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")

    @app.get("/")
    def console() -> Any:
        return send_from_directory(STATIC_DIR / "console", "index.html")

    @app.get("/editor")
    def editor() -> Any:
        return send_from_directory(STATIC_DIR / "editor", "index.html")

    @app.get("/data/runs/<path:relpath>")
    def run_artifact(relpath: str) -> Any:
        # 仅允许访问运行产物目录内的文件（截图等证据）
        runs_root = (ROOT / "data" / "runs").resolve()
        target = (runs_root / relpath).resolve()
        if runs_root not in target.parents or not target.is_file():
            return jsonify({"error": "证据文件不存在"}), 404
        return send_from_directory(runs_root, relpath)

    @app.get("/api/tasks")
    def list_tasks() -> Any:
        import json

        return jsonify(json.loads((TASKS_DIR / "index.json").read_text(encoding="utf-8")))

    @app.get("/api/tasks/<task_id>")
    def get_task(task_id: str) -> Any:
        try:
            raw = _load_task_raw(task_id)
            parse_task(raw)  # 出口前再次协议校验
        except FileNotFoundError:
            return jsonify({"error": f"任务不存在: {task_id}"}), 404
        except ValueError as exc:
            return jsonify({"error": f"任务协议校验失败: {exc}"}), 422
        return jsonify(raw)

    @app.get("/api/editor/<task_id>")
    def get_editor_view(task_id: str) -> Any:
        try:
            task = parse_task(_load_task_raw(task_id))
        except FileNotFoundError:
            return jsonify({"error": f"任务不存在: {task_id}"}), 404
        except ValueError as exc:
            return jsonify({"error": f"任务协议校验失败: {exc}"}), 422
        return jsonify(editor_view(task))

    @app.get("/api/truth-available")
    def truth_gate() -> Any:
        """说明性接口：真值目录存在但采集器禁用；控制台不展示真值内容。"""
        return jsonify({"truth_dir_exists": TRUTH_DIR.exists(), "collector_access": "forbidden"})

    def _execute(job_id: str, task_id: str, base_url: str, headless: bool) -> None:
        try:
            with _jobs_lock:
                _jobs[job_id]["status"] = "RUNNING"
            automation = CurveEditorAutomation(base_url=base_url, headless=headless)
            result = automation.run(task_id)
            payload = {
                "task_id": result.task_id,
                "status": result.status,
                "failure_reason": result.failure_reason,
                "preflight": result.preflight,
                "mapping": result.mapping,
                "point_results": result.point_results,
                "evidence": result.evidence,
                "iterations_total": result.iterations_total,
            }
            with _jobs_lock:
                _jobs[job_id].update({"status": "DONE", "result": payload})
        except Exception as exc:  # noqa: BLE001 - 控制台需要如实呈现具体错误
            with _jobs_lock:
                _jobs[job_id].update(
                    {
                        "status": "ERROR",
                        "error": {"type": type(exc).__name__, "message": str(exc), "trace": traceback.format_exc()},
                    }
                )

    @app.post("/api/run")
    def run_task() -> Any:
        body = request.get_json(silent=True) or {}
        task_id = body.get("task_id")
        headless = bool(body.get("headless", True))
        if not isinstance(task_id, str) or not task_id:
            return jsonify({"error": "缺少 task_id"}), 400
        try:
            parse_task(_load_task_raw(task_id))
        except FileNotFoundError:
            return jsonify({"error": f"任务不存在: {task_id}"}), 404
        except ValueError as exc:
            return jsonify({"error": f"任务协议校验失败: {exc}"}), 422

        job_id = uuid.uuid4().hex[:12]
        base_url = request.host_url.rstrip("/")
        with _jobs_lock:
            _jobs[job_id] = {"job_id": job_id, "task_id": task_id, "status": "QUEUED"}
        thread = threading.Thread(target=_execute, args=(job_id, task_id, base_url, headless), daemon=True)
        thread.start()
        return jsonify({"job_id": job_id})

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str) -> Any:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if not job:
                return jsonify({"error": "任务执行记录不存在"}), 404
            return jsonify(job)

    @app.get("/api/jobs")
    def list_jobs() -> Any:
        with _jobs_lock:
            jobs = sorted(_jobs.values(), key=lambda j: j["job_id"], reverse=True)
            return jsonify({"jobs": jobs})

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="曲线编辑器 RPA 本地控制台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def serve(host: str, port: int) -> None:
    """使用标准库多线程 WSGI 服务器运行，避免 Flask 开发服务器在 RPA 嵌套请求下不稳定。"""
    from wsgiref.simple_server import WSGIRequestHandler, WSGIServer
    from socketserver import ThreadingMixIn

    class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True
        allow_reuse_address = True

    class QuietHandler(WSGIRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # 降噪，防止日志写满管道
            pass

    app = create_app()
    httpd = ThreadingWSGIServer((host, port), QuietHandler)
    httpd.set_app(app)
    print(f"[curve-rpa] 控制台已启动：http://{host}:{port}/ （Ctrl+C 退出）", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
