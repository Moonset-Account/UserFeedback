"""采集控制 API 蓝图：配置输入、运行任务、查看结果/证据/错误。"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

api_bp = Blueprint("api", __name__, url_prefix="/api")


def _manager():
    return current_app.config["TASK_MANAGER"]


@api_bp.get("/scenarios")
def scenarios():
    return jsonify({"scenarios": _manager().list_scenarios()})


@api_bp.post("/tasks")
def create_task():
    body = request.get_json(silent=True) or {}
    scenario_key = body.get("scenario_key")
    transport = body.get("transport", "http")
    try:
        task = _manager().create_task(scenario_key, transport)
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"task_id": task.task_id, "status": task.status}), 202


@api_bp.get("/tasks")
def list_tasks():
    return jsonify({"tasks": _manager().all_tasks()})


@api_bp.get("/tasks/<task_id>")
def get_task(task_id: str):
    task = _manager().get_task(task_id)
    if task is None:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(task.public_view())


@api_bp.post("/demo")
def run_demo():
    """运行示例按钮：依次跑正常场景与边界场景，返回两个任务 id。"""
    transport = (request.get_json(silent=True) or {}).get("transport", "http")
    manager = _manager()
    task_ids = []
    for key in ("demo_normal", "demo_edge"):
        task = manager.create_task(key, transport)
        task_ids.append(task.task_id)
    return jsonify({"task_ids": task_ids}), 202
