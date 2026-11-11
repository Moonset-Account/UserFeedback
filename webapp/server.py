"""网页服务端：配置输入、运行任务、查看来源证据与具体错误。

运行记录保存在内存中（进程内），任务在后台线程执行，前端轮询取得完整事件流。
采集器只通过源站公开页面/接口交互；/admin/* 仅用于演示前重置链接状态。
"""
from __future__ import annotations

import threading
from pathlib import Path

import requests
from flask import Blueprint, current_app, jsonify, render_template, request

from collector.engine import CollectionEngine
from collector.http_backend import HttpBackend
from collector.playwright_backend import PlaywrightBackend
from generator.scenarios import DEMO_SCENARIOS, SCENARIOS, SCENARIO_ORDER

bp = Blueprint("web", __name__)

_LOCK = threading.RLock()
_RUNS: dict[str, dict] = {}
_ORDER: list[str] = []
_MAX_RUNS = 50


def _config() -> dict:
    return current_app.config["COLLECTOR_CONFIG"]


def _page_url(source_base: str, scenario_id: str) -> str:
    return f"{source_base.rstrip('/')}/experiment/{scenario_id}"


def _reset_source(source_base: str) -> str | None:
    try:
        resp = requests.post(f"{source_base.rstrip('/')}/admin/reset", timeout=5)
        resp.raise_for_status()
        return None
    except requests.RequestException as exc:
        return f"源站重置失败（不影响继续尝试）：{exc}"


def _store_run(result_dict: dict) -> None:
    with _LOCK:
        _RUNS[result_dict["run_id"]] = result_dict
        _ORDER.append(result_dict["run_id"])
        for stale in _ORDER[:-_MAX_RUNS]:
            _RUNS.pop(stale, None)
        del _ORDER[:-_MAX_RUNS]


def _execute(cfg: dict, run_id: str, scenario_id: str, backend_name: str,
             max_refreshes: int, reset_first: bool) -> None:
    source_base = cfg["source_base_url"]
    warnings: list[str] = []
    if reset_first:
        warn = _reset_source(source_base)
        if warn:
            warnings.append(warn)

    backend = PlaywrightBackend() if backend_name == "playwright" else HttpBackend()
    engine = CollectionEngine(
        backend,
        downloads_dir=Path(cfg["data_dir"]) / "downloads",
        max_refreshes=max_refreshes,
    )
    try:
        result = engine.run(scenario_id, _page_url(source_base, scenario_id), run_id=run_id)
        payload = result.as_dict()
    except Exception as exc:  # 兜底：任何意外都如实展示，不预填成功
        payload = {
            "run_id": run_id,
            "scenario_id": scenario_id,
            "backend_name": backend_name,
            "status": "failed_exception",
            "message": f"采集器内部异常: {exc!r}",
            "attempts": 0, "refreshes": 0, "max_refreshes": max_refreshes,
            "events": [], "saved_path": None, "validation": None, "duration_ms": 0,
        }
    finally:
        backend.close()

    if warnings:
        payload.setdefault("events", []).insert(
            0, {"seq": 0, "kind": "warning", "message": "；".join(warnings), "ts": 0})
    _store_run(payload)


def _start_thread(target, *args) -> None:
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()


# --------------------------------------------------------------------------
# 页面与接口
# --------------------------------------------------------------------------
@bp.get("/")
def home():
    cfg = _config()
    return render_template(
        "index.html",
        scenarios=[SCENARIOS[sid] for sid in SCENARIO_ORDER],
        demo_scenarios=DEMO_SCENARIOS,
        source_base_url=cfg["source_base_url"],
        default_max_refreshes=cfg["default_max_refreshes"],
    )


@bp.get("/api/scenarios")
def api_scenarios():
    cfg = _config()
    return jsonify({
        "scenarios": [
            {
                "id": sid,
                "title": SCENARIOS[sid].title,
                "description": SCENARIOS[sid].description,
                "js_rendered": SCENARIOS[sid].js_rendered,
                "page_url": _page_url(cfg["source_base_url"], sid),
            }
            for sid in SCENARIO_ORDER
        ],
        "demo_scenarios": DEMO_SCENARIOS,
        "source_base_url": cfg["source_base_url"],
    })


@bp.post("/api/runs")
def create_run():
    body = request.get_json(force=True, silent=True) or {}
    scenario_id = body.get("scenario_id", "")
    backend_name = body.get("backend", "http")
    if scenario_id not in SCENARIOS:
        return jsonify({"error": "unknown_scenario", "scenario_id": scenario_id}), 400
    if backend_name not in ("http", "playwright"):
        return jsonify({"error": "unknown_backend", "backend": backend_name}), 400

    max_refreshes = int(body.get("max_refreshes") or _config()["default_max_refreshes"])
    max_refreshes = max(0, min(max_refreshes, 10))
    reset_first = bool(body.get("reset_first", True))

    # 先建一个占位记录，前端立即获得轮询目标；任务结束后覆盖
    placeholder = {
        "run_id": "pending-" + scenario_id + "-" + str(len(_ORDER)),
        "scenario_id": scenario_id,
        "backend_name": backend_name,
        "status": "running",
        "message": "任务排队/执行中…",
        "attempts": 0, "refreshes": 0, "max_refreshes": max_refreshes,
        "events": [], "saved_path": None, "validation": None, "duration_ms": 0,
    }
    _store_run(placeholder)
    _start_thread(_execute, _config(), placeholder["run_id"], scenario_id,
                  backend_name, max_refreshes, reset_first)
    return jsonify({"run_id": placeholder["run_id"], "status": "running"})


@bp.post("/api/demo")
def run_demo():
    """依次运行一个正常场景与一个边界场景（重置源站保证确定性）。"""
    cfg = _config()
    max_refreshes = cfg["default_max_refreshes"]
    job_id = "demo-" + str(len(_ORDER))
    _store_run({
        "run_id": job_id, "scenario_id": "demo-batch", "backend_name": "http",
        "status": "running", "message": "示例批次执行中…",
        "attempts": 0, "refreshes": 0, "max_refreshes": max_refreshes,
        "events": [], "saved_path": None, "validation": None, "duration_ms": 0,
    })

    def _batch() -> None:
        child_ids: list[str] = []
        for sid in DEMO_SCENARIOS:
            _reset_source(cfg["source_base_url"])
            backend = HttpBackend()
            engine = CollectionEngine(
                backend,
                downloads_dir=Path(cfg["data_dir"]) / "downloads",
                max_refreshes=max_refreshes,
            )
            try:
                result = engine.run(sid, _page_url(cfg["source_base_url"], sid)).as_dict()
            except Exception as exc:
                result = {"run_id": f"error-{sid}", "scenario_id": sid, "backend_name": "http",
                          "status": "failed_exception", "message": repr(exc),
                          "attempts": 0, "refreshes": 0, "max_refreshes": max_refreshes,
                          "events": [], "saved_path": None, "validation": None, "duration_ms": 0}
            finally:
                backend.close()
            _store_run(result)
            child_ids.append(result["run_id"])
        with _LOCK:
            _RUNS[job_id].update({
                "status": "completed",
                "message": f"示例批次完成：{', '.join(DEMO_SCENARIOS)}",
                "child_run_ids": child_ids,
            })

    _start_thread(_batch)
    return jsonify({"job_id": job_id, "scenarios": DEMO_SCENARIOS})


@bp.get("/api/runs")
def list_runs():
    with _LOCK:
        items = [_RUNS[rid] for rid in reversed(_ORDER) if rid in _RUNS]
    return jsonify({"runs": items})


@bp.get("/api/runs/<run_id>")
def get_run(run_id: str):
    with _LOCK:
        run = _RUNS.get(run_id)
    if run is None:
        return jsonify({"error": "not_found", "run_id": run_id}), 404
    return jsonify(run)


@bp.post("/api/runs/clear")
def clear_runs():
    with _LOCK:
        _RUNS.clear()
        _ORDER.clear()
    return jsonify({"ok": True})
