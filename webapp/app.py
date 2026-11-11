"""Web 主应用：中文操作入口的后端 API。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from .frequency import run_frequency_check
from .task_manager import TaskManager

STATIC_DIR = Path(__file__).resolve().parent / "static"

# 预置场景（演示链 + 更完整验收场景的选择入口）
SCENARIOS = {
    "demo-normal": {
        "label": "示例①正常场景：预算充足、完整扫描（demo 数据集）",
        "start_url_path": "/api/catalog",
        "seed": 20261111,
        "sample_size": 12,
        "request_budget": 30,
        "transport": "http",
    },
    "demo-budget": {
        "label": "示例②边界场景：预算中途耗尽（仅观察前 4 页）",
        "start_url_path": "/api/catalog",
        "seed": 20261111,
        "sample_size": 10,
        "request_budget": 4,
        "transport": "http",
    },
    "large-full": {
        "label": "验收场景 A：large 数据集完整扫描 + 跨页重复去重",
        "start_url_path": "/api/catalog?dataset=large",
        "seed": 777,
        "sample_size": 25,
        "request_budget": 60,
        "transport": "http",
    },
    "large-budget": {
        "label": "验收场景 B：large 数据集预算耗尽（扫描边界证据）",
        "start_url_path": "/api/catalog?dataset=large",
        "seed": 777,
        "sample_size": 25,
        "request_budget": 7,
        "transport": "http",
    },
    "sample-exceeds": {
        "label": "验收场景 C：样本量大于总体（应全量收录、无替换）",
        "start_url_path": "/api/catalog",
        "seed": 42,
        "sample_size": 500,
        "request_budget": 30,
        "transport": "http",
    },
    "html-browser": {
        "label": "验收场景 D：HTML 协议子集 + Playwright 浏览器采集",
        "start_url_path": "/catalog",
        "seed": 99,
        "sample_size": 8,
        "request_budget": 30,
        "transport": "browser",
    },
}


def create_app(source_base_url: str) -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    manager = TaskManager()
    app.config["SOURCE_BASE_URL"] = source_base_url.rstrip("/")
    app.config["TASK_MANAGER"] = manager

    def resolve_url(path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        if not path_or_url.startswith("/"):
            path_or_url = "/" + path_or_url
        return app.config["SOURCE_BASE_URL"] + path_or_url

    @app.get("/")
    def index() -> Any:
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/api/meta")
    def meta() -> Any:
        return jsonify(
            {
                "source_base_url": app.config["SOURCE_BASE_URL"],
                "scenarios": {k: {kk: vv for kk, vv in v.items()} for k, v in SCENARIOS.items()},
                "protocol_subset": [
                    "HTTP/HTTPS GET；JSON：{items:[{id,...}], next_cursor}（无 total 字段）",
                    "HTML：div[data-role=catalog] > article.specimen[data-id]，a[data-role=next-page]",
                    "不支持 XML/POST 翻页/鉴权接口；JS 渲染页走 Playwright 通道",
                ],
            }
        )

    @app.post("/api/tasks")
    def create_task() -> Any:
        data = request.get_json(force=True, silent=True) or {}
        try:
            seed = int(data["seed"])
            sample_size = int(data["sample_size"])
            request_budget = int(data["request_budget"])
            start_url = resolve_url(str(data["start_url"]))
        except (KeyError, TypeError, ValueError) as exc:
            return jsonify({"error": f"参数无效：{exc}"}), 400
        if sample_size < 0 or request_budget <= 0:
            return jsonify({"error": "sample_size 需 >=0，request_budget 需为正整数"}), 400
        transport = str(data.get("transport", "http"))
        if transport not in ("http", "browser"):
            return jsonify({"error": "transport 仅支持 http / browser"}), 400
        handle = manager.submit(
            label=str(data.get("label", "自定义任务")),
            start_url=start_url, seed=seed, sample_size=sample_size,
            request_budget=request_budget, transport=transport,
            headless=bool(data.get("headless", True)),
            timeout=float(data.get("timeout", 8.0)),
            max_retries=int(data.get("max_retries", 2)),
        )
        return jsonify(handle.to_dict()), 202

    @app.get("/api/tasks")
    def list_tasks() -> Any:
        return jsonify(manager.list_tasks())

    @app.get("/api/tasks/<task_id>")
    def get_task(task_id: str) -> Any:
        handle = manager.get(task_id)
        if handle is None:
            return jsonify({"error": "任务不存在"}), 404
        return jsonify(handle.to_dict())

    @app.post("/api/demo")
    def run_demo() -> Any:
        if manager.demo_busy:
            return jsonify({"error": "演示链正在运行中，请等待结束"}), 409
        jobs = []
        for key in ("demo-normal", "demo-budget"):
            sc = SCENARIOS[key]
            jobs.append(
                {
                    "label": sc["label"],
                    "start_url": resolve_url(sc["start_url_path"]),
                    "seed": sc["seed"],
                    "sample_size": sc["sample_size"],
                    "request_budget": sc["request_budget"],
                    "transport": sc["transport"],
                }
            )
        chain_id = manager.start_demo_chain(jobs)
        return jsonify({"chain_id": chain_id, "jobs": jobs}), 202

    @app.post("/api/scenarios/<name>")
    def run_scenario(name: str) -> Any:
        if name not in SCENARIOS:
            return jsonify({"error": f"未知场景，可选：{sorted(SCENARIOS)}"}), 404
        sc = SCENARIOS[name]
        handle = manager.submit(
            label=sc["label"],
            start_url=resolve_url(sc["start_url_path"]),
            seed=sc["seed"], sample_size=sc["sample_size"],
            request_budget=sc["request_budget"], transport=sc["transport"],
        )
        return jsonify(handle.to_dict()), 202

    @app.post("/api/frequency")
    def frequency() -> Any:
        data = request.get_json(force=True, silent=True) or {}
        try:
            result = run_frequency_check(
                population_size=int(data.get("population_size", 25)),
                sample_size=int(data.get("sample_size", 5)),
                trials=int(data.get("trials", 2000)),
                base_seed=int(data.get("base_seed", 20261111)),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(result)

    return app
