"""Flask 服务：模拟实验台（被测系统）+ RPA 控制台。

路由分两类：
- /lab/...   被测实验台页面与公开只读/操作接口（RPA 采集器唯一允许访问的边界）；
- /、/api/... 控制台页面与任务管理接口。
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

from flask import Blueprint, Flask, jsonify, request, send_from_directory

from ..rpa.errors import RpaError
from ..rpa.executor import run_task
from ..rpa.protocol import parse_task
from ..sample_data.generator import ensure_generated, list_scenarios
from . import lab_registry

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
SAMPLES_DIR = DATA_DIR / "samples"
GROUND_TRUTH_DIR = DATA_DIR / "ground_truth"
RUNS_DIR = DATA_DIR / "runs"
STATIC_DIR = Path(__file__).parent / "static"

# ---------------- 模拟实验台状态（仅进程内虚构状态） ----------------
_lab_lock = threading.Lock()
_lab_state: dict = {
    "context": "ALPHA",
    "devices": dict(lab_registry.INITIAL_DEVICE_STATES),
    "preset": None,
}
_run_lock = threading.Lock()
_runs: dict[str, dict] = {}


lab_bp = Blueprint("lab", __name__, url_prefix="/lab")


@lab_bp.get("/")
def lab_page():
    return send_from_directory(STATIC_DIR / "lab", "lab.html")


@lab_bp.get("/static/<path:filename>")
def lab_static(filename):
    return send_from_directory(STATIC_DIR / "lab", filename)


@lab_bp.get("/api/health")
def lab_health():
    return jsonify({"ok": True, "service": "simulated-lab", "real_device": False})


@lab_bp.get("/api/snapshot")
def lab_snapshot():
    """公开只读快照接口：菜单结构 + 禁用规则 + 当前可见状态。"""
    with _lab_lock:
        return jsonify(
            lab_registry.snapshot(
                _lab_state["context"], _lab_state["devices"], _lab_state["preset"]
            )
        )


@lab_bp.post("/api/context")
def lab_switch_context():
    body = request.get_json(silent=True) or {}
    context = body.get("context")
    if context not in lab_registry.CONTEXTS:
        return jsonify({"ok": False, "error": f"未知上下文 {context!r}"}), 400
    with _lab_lock:
        _lab_state["context"] = context
        # 切换上下文后预设清空，设备保持当前状态（页面可见）。
        _lab_state["preset"] = None
    return jsonify({"ok": True, "context": context})


@lab_bp.post("/api/reset")
def lab_reset():
    """复位到确定的初始虚构状态，供 RPA 每个会话开始时建立干净基线。"""
    with _lab_lock:
        _lab_state["context"] = "ALPHA"
        _lab_state["devices"] = dict(lab_registry.INITIAL_DEVICE_STATES)
        _lab_state["preset"] = None
    return jsonify({"ok": True, "state": _lab_state})


@lab_bp.post("/api/command")
def lab_command():
    """模拟命令执行：只改本地虚构状态，服务端二次校验禁用规则。"""
    body = request.get_json(silent=True) or {}
    command_id = body.get("command_id")
    with _lab_lock:
        context = _lab_state["context"]
        if (context, command_id) in lab_registry.DISABLE_RULES:
            return jsonify(
                {
                    "ok": False,
                    "error": lab_registry.DISABLE_RULES[(context, command_id)],
                }
            ), 409

        # 动态预设命令。
        preset = next(
            (
                p
                for p in lab_registry.DYNAMIC_PRESETS[context]
                if p["id"] == command_id
            ),
            None,
        )
        if preset is not None:
            _lab_state["preset"] = preset["preset_key"]
            return jsonify({"ok": True, "event": f"应用实验预设：{preset['label']}"})

        if command_id in lab_registry.DEVICE_COMMAND_RESULTS:
            device, new_state = lab_registry.DEVICE_COMMAND_RESULTS[command_id]
            old_state = _lab_state["devices"][device]
            _lab_state["devices"][device] = new_state
            return jsonify(
                {
                    "ok": True,
                    "event": f"{device}：{old_state} → {new_state}（命令 {command_id}）",
                }
            )
    return jsonify({"ok": False, "error": f"未知命令 {command_id!r}"}), 404


# ---------------- RPA 控制台 ----------------
console_bp = Blueprint("console", __name__)


@console_bp.get("/")
def console_page():
    return send_from_directory(STATIC_DIR / "console", "index.html")


@console_bp.get("/console-static/<path:filename>")
def console_static(filename):
    return send_from_directory(STATIC_DIR / "console", filename)


@console_bp.get("/api/scenarios")
def api_scenarios():
    return jsonify(
        {
            "scenarios": list_scenarios(),
            "ground_truth_note": (
                "ground_truth 仅供测试断言使用，RPA 采集器与执行器代码不得读取该目录"
            ),
        }
    )


@console_bp.get("/api/scenario-content")
def api_scenario_content():
    """返回样例任务原文，供控制台载入编辑（不暴露 ground_truth）。"""
    name = request.args.get("file", "")
    task_path = (SAMPLES_DIR / name).resolve()
    if SAMPLES_DIR.resolve() not in task_path.parents or not task_path.exists():
        return jsonify({"ok": False, "error": "样例文件不存在"}), 404
    return jsonify(
        {"ok": True, "file": name, "document": json.loads(task_path.read_text(encoding="utf-8"))}
    )


@console_bp.post("/api/run")
def api_run():
    """运行任务。body: {"task_file": 场景文件名} 或 {"task_document": {...}}。"""
    body = request.get_json(silent=True) or {}
    headless = bool(body.get("headless", True))
    if "task_file" in body:
        task_path = (SAMPLES_DIR / body["task_file"]).resolve()
        if SAMPLES_DIR.resolve() not in task_path.parents or not task_path.exists():
            return jsonify({"ok": False, "error": "任务文件不在样例目录内"}), 400
        document = task_path.read_text(encoding="utf-8")
        source_name = task_path.name
    elif "task_document" in body:
        document = json.dumps(body["task_document"], ensure_ascii=False)
        source_name = "自定义任务"
    else:
        return jsonify({"ok": False, "error": "需要 task_file 或 task_document"}), 400

    try:
        task = parse_task(document)
    except RpaError as exc:
        return jsonify({"ok": False, "error": exc.to_dict()}), 422

    run_id = uuid.uuid4().hex[:12]
    run_dir = RUNS_DIR / run_id
    record = {
        "run_id": run_id,
        "task_name": task.name,
        "source": source_name,
        "status": "running",
        "report": None,
        "error": None,
    }
    with _run_lock:
        _runs[run_id] = record

    base_url = request.host_url.rstrip("/")
    thread = threading.Thread(
        target=_run_worker,
        args=(run_id, task, base_url, run_dir, headless),
        daemon=True,
    )
    thread.start()
    return jsonify({"ok": True, "run_id": run_id})


def _run_worker(run_id, task, base_url, run_dir, headless):
    try:
        report = run_task(task, base_url, run_dir, headless=headless)
        with _run_lock:
            _runs[run_id]["status"] = report.status
            _runs[run_id]["report"] = report.to_dict()
    except RpaError as exc:
        with _run_lock:
            _runs[run_id]["status"] = "failed"
            _runs[run_id]["error"] = exc.to_dict()
    except Exception as exc:  # noqa: BLE001 - 网页需要看到具体错误
        with _run_lock:
            _runs[run_id]["status"] = "failed"
            _runs[run_id]["error"] = {
                "code": "UNEXPECTED_ERROR",
                "message": f"{type(exc).__name__}: {exc}",
                "detail": {},
            }


@console_bp.get("/api/runs")
def api_runs():
    with _run_lock:
        items = sorted(_runs.values(), key=lambda r: r["run_id"], reverse=True)
    return jsonify({"runs": items})


@console_bp.get("/api/runs/<run_id>")
def api_run_detail(run_id):
    with _run_lock:
        record = _runs.get(run_id)
    if record is None:
        return jsonify({"ok": False, "error": "运行记录不存在"}), 404
    return jsonify(record)


@console_bp.get("/api/runs/<run_id>/evidence/<path:filename>")
def api_run_evidence(run_id, filename):
    run_dir = (RUNS_DIR / run_id / "evidence").resolve()
    target = (run_dir / filename).resolve()
    if run_dir not in target.parents:
        return jsonify({"ok": False, "error": "非法路径"}), 400
    if not target.exists():
        return jsonify({"ok": False, "error": "证据文件不存在"}), 404
    return send_from_directory(run_dir, filename)


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(lab_bp)
    app.register_blueprint(console_bp)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return app


def ensure_samples() -> None:
    ensure_generated(SAMPLES_DIR, GROUND_TRUTH_DIR)
