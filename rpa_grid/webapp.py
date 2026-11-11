"""Flask 服务：中文操作入口 + 样例公开接口 + 任务 API。

路由说明（采集边界）：
- /samples/<scenario>/matrix.tsv   采集器唯一的矩阵数据接口
- /samples/<scenario>/schema.json  列声明（无期望值）
- /samples/<scenario>/grid_config.json / scenario.json 结构与说明
- truth.json 不注册任何路由，外部与采集器均无法经 HTTP 获取。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from flask import Flask, Response, abort, jsonify, render_template, request

from .runner import TaskRegistry, run_task
from .sample_generator import generate_all

BASE_DIR = Path(__file__).resolve().parent.parent
SAMPLES_DIR = BASE_DIR / "data" / "samples"
ALLOWED_SCENARIOS = {"demo-normal", "demo-edge", "acceptance-full"}


def create_app() -> Flask:
    app = Flask(__name__, template_folder=str(BASE_DIR / "templates"),
                static_folder=str(BASE_DIR / "static"))
    registry = TaskRegistry()
    app.config["REGISTRY"] = registry

    if not SAMPLES_DIR.exists() or not any(SAMPLES_DIR.iterdir()):
        generate_all(SAMPLES_DIR)

    @app.after_request
    def _no_cache(resp: Response) -> Response:
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---------- 页面 ----------
    @app.route("/")
    def index() -> str:
        return render_template("index.html")

    @app.route("/grid")
    def grid_page() -> str:
        scenario = request.args.get("scenario", "demo-normal")
        if scenario not in ALLOWED_SCENARIOS:
            abort(404)
        return render_template("grid.html")

    # ---------- 公开样例接口（采集器只允许读这里） ----------
    def _sample_file(scenario: str, name: str,
                     content_type: str) -> Response:
        if scenario not in ALLOWED_SCENARIOS:
            abort(404)
        path = SAMPLES_DIR / scenario / name
        if not path.exists():
            abort(404)
        # 字节返回，避免通用换行改写 \r
        return Response(path.read_bytes(), status=200,
                        content_type=content_type)

    @app.route("/samples/<scenario>/matrix.tsv")
    def sample_matrix(scenario: str) -> Response:
        return _sample_file(scenario, "matrix.tsv",
                            "text/tab-separated-values; charset=utf-8")

    @app.route("/samples/<scenario>/<name>.json")
    def sample_json(scenario: str, name: str) -> Response:
        if name == "truth":
            abort(403)  # 真值禁止经接口暴露
        if name not in ("schema", "grid_config", "scenario"):
            abort(404)
        return _sample_file(scenario, f"{name}.json",
                            "application/json; charset=utf-8")

    @app.route("/api/scenarios")
    def api_scenarios() -> Response:
        out = []
        for s in sorted(ALLOWED_SCENARIOS):
            meta = json.loads(
                (SAMPLES_DIR / s / "scenario.json").read_text("utf-8"))
            cfg = json.loads(
                (SAMPLES_DIR / s / "grid_config.json").read_text("utf-8"))
            out.append({"name": s, "title": meta["title"],
                        "description": meta["description"],
                        "hidden_columns": cfg["hidden_columns"],
                        "readonly_cells": cfg["readonly_cells"]})
        return jsonify(out)

    # ---------- 任务 API ----------
    # 单线程执行器：所有 RPA 任务排队到同一个长期工作线程。
    # Playwright sync API 的事件循环绑定线程；让每个任务的
    # sync_playwright() 在同一线程里「完整创建并关闭」，可安全顺序复用，
    # 而嵌套临时线程反而会因 greenlet 归属问题挂起。
    from concurrent.futures import ThreadPoolExecutor

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rpa-worker")

    @app.route("/api/tasks", methods=["GET"])
    def list_tasks() -> Response:
        return jsonify(registry.list())

    @app.route("/api/tasks/<task_id>", methods=["GET"])
    def get_task(task_id: str) -> Response:
        task = registry.get(task_id)
        if not task:
            abort(404)
        return jsonify(task)

    @app.route("/api/run", methods=["POST"])
    def api_run() -> Response:
        payload: Dict[str, Any] = request.get_json(silent=True) or {}
        scenario = payload.get("scenario", "")
        headless = bool(payload.get("headless", True))
        if scenario not in ALLOWED_SCENARIOS:
            return jsonify({"error": f"未知场景: {scenario}"}), 400
        base_url = request.host_url.rstrip("/")
        # 提交到单线程执行器，接口立刻返回 task_id，前端轮询
        executor.submit(
            lambda: run_task(registry=registry, base_url=base_url,
                             scenario=scenario, headless=headless))
        # 让工作线程先注册任务
        import time as _t
        _t.sleep(0.2)
        tasks = [x for x in registry.list() if x["scenario"] == scenario]
        return jsonify({"task_id": tasks[0]["task_id"], "status": "queued"})

    @app.route("/api/demo", methods=["POST"])
    def api_demo() -> Response:
        """运行示例：排队执行 正常 -> 边界 两个小样例，保留两份结果。"""
        base_url = request.host_url.rstrip("/")
        headless = bool((request.get_json(silent=True) or {}).get("headless", True))

        def worker() -> None:
            for scenario in ("demo-normal", "demo-edge"):
                run_task(registry, base_url, scenario, headless=headless)

        executor.submit(worker)
        return jsonify({"started": True,
                        "scenarios": ["demo-normal", "demo-edge"]})

    @app.route("/api/evidence/<task_id>/screenshot")
    def task_screenshot(task_id: str) -> Response:
        task = registry.get(task_id)
        if not task or not task.get("result"):
            abort(404)
        shot = Path(task["result"]["evidence"]["screenshot"])
        if not shot.exists():
            abort(404)
        return Response(shot.read_bytes(), content_type="image/png")

    return app
