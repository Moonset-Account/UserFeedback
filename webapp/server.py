"""Flask 后端：样例选择、SVG 上传、采集执行、结果与证据查询。

采集只通过 collector 包的公开接口 collect()/collect_file() 进行；
真值文件 samples/truth/ 不会被任何路由读取。
"""

from __future__ import annotations

import json
import os
import time
import uuid

from flask import (Flask, abort, jsonify, request, send_file,
                   send_from_directory)

from collector import PROTOCOL_SUBSET, collect
from generator.scenarios import SCENARIOS

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES_DIR = os.path.join(BASE_DIR, "samples")
RUNS_DIR = os.path.join(BASE_DIR, "runs")
STATIC_DIR = os.path.join(BASE_DIR, "webapp", "static")

os.makedirs(RUNS_DIR, exist_ok=True)

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8MB 上限


def _run_path(run_id: str) -> str:
    # 防路径穿越
    safe = "".join(ch for ch in run_id if ch.isalnum() or ch in "-_")
    path = os.path.join(RUNS_DIR, f"{safe}.json")
    if not os.path.abspath(path).startswith(os.path.abspath(RUNS_DIR)):
        abort(404)
    return path


def _save_run(payload: dict) -> str:
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    payload["run_id"] = run_id
    payload["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(_run_path(run_id), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return run_id


def _execute(svg_text: str, source_name: str, file_size: int,
             scenario_key: str | None = None) -> dict:
    started = time.perf_counter()
    result = collect(svg_text, source_name=source_name, file_size=file_size)
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    result["scenario_key"] = scenario_key
    result["run_id"] = _save_run(result)
    result["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return result


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/scenarios")
def api_scenarios():
    """场景清单（不暴露真值）。"""
    items = []
    for key, (title, _func) in SCENARIOS.items():
        svg_path = os.path.join(SAMPLES_DIR, f"{key}.svg")
        items.append({
            "key": key,
            "title": title,
            "svg_url": f"/svg/sample/{key}.svg",
            "available": os.path.exists(svg_path),
        })
    return jsonify({"scenarios": items})


@app.route("/svg/sample/<path:filename>")
def sample_svg(filename):
    # 仅允许 samples 目录下的 .svg，禁止访问 truth/
    if not filename.endswith(".svg") or "/" in filename or ".." in filename:
        abort(404)
    return send_from_directory(SAMPLES_DIR, filename, mimetype="image/svg+xml")


@app.route("/api/run/sample", methods=["POST"])
def api_run_sample():
    key = (request.get_json(silent=True) or {}).get("scenario")
    if key not in SCENARIOS:
        return jsonify({"error": f"未知场景: {key!r}"}), 400
    svg_path = os.path.join(SAMPLES_DIR, f"{key}.svg")
    if not os.path.exists(svg_path):
        return jsonify({"error": "样例尚未生成，请先运行 build_samples.py"}), 400
    with open(svg_path, "rb") as fh:
        data = fh.read()
    result = _execute(data.decode("utf-8"), f"{key}.svg", len(data),
                      scenario_key=key)
    result["svg_url"] = f"/svg/sample/{key}.svg"
    # 重新持久化一次，把 svg_url 写入
    with open(_run_path(result["run_id"]), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    return jsonify(result)


@app.route("/api/run/upload", methods=["POST"])
def api_run_upload():
    file = request.files.get("svg")
    if file is None or not file.filename:
        return jsonify({"error": "未收到 SVG 文件"}), 400
    if not file.filename.lower().endswith(".svg"):
        return jsonify({"error": "仅接受 .svg 文件"}), 400
    data = file.read()
    if len(data) > app.config["MAX_CONTENT_LENGTH"]:
        return jsonify({"error": "文件超过 8MB 限制"}), 400
    try:
        svg_text = data.decode("utf-8")
    except UnicodeDecodeError:
        return jsonify({"error": "SVG 必须是 UTF-8 编码的文本"}), 400
    # 上传文件落盘到 runs 目录，供页面渲染与“修改后重采”
    run_stub = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    stored_name = f"upload-{run_stub}.svg"
    stored_path = os.path.join(RUNS_DIR, stored_name)
    with open(stored_path, "w", encoding="utf-8") as fh:
        fh.write(svg_text)
    result = _execute(svg_text, file.filename, len(data))
    result["svg_url"] = f"/svg/upload/{stored_name}"
    result["stored_svg"] = stored_name
    with open(_run_path(result["run_id"]), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    return jsonify(result)


@app.route("/svg/upload/<path:filename>")
def upload_svg(filename):
    if not filename.endswith(".svg") or "/" in filename or ".." in filename:
        abort(404)
    return send_from_directory(RUNS_DIR, filename, mimetype="image/svg+xml")


@app.route("/api/rerun", methods=["POST"])
def api_rerun():
    """对已保存的 SVG 重新采集（用户在外部修改文件后调用）。"""
    body = request.get_json(silent=True) or {}
    stored = body.get("stored_svg")
    if not stored or "/" in stored or ".." in stored \
            or not stored.endswith(".svg"):
        return jsonify({"error": "stored_svg 参数非法"}), 400
    path = os.path.join(RUNS_DIR, stored)
    if not os.path.exists(path):
        return jsonify({"error": "找不到原始 SVG，可能已被清理，请重新上传"}), 404
    with open(path, "rb") as fh:
        data = fh.read()
    result = _execute(data.decode("utf-8"), stored, len(data))
    result["svg_url"] = f"/svg/upload/{stored}"
    result["stored_svg"] = stored
    with open(_run_path(result["run_id"]), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    return jsonify(result)


@app.route("/api/runs")
def api_runs():
    items = []
    for name in sorted(os.listdir(RUNS_DIR), reverse=True):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(RUNS_DIR, name), encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        items.append({
            "run_id": payload.get("run_id", name[:-5]),
            "created_at": payload.get("created_at"),
            "source": payload.get("source", {}).get("name"),
            "status": payload.get("status"),
            "summary": payload.get("summary"),
            "scenario_key": payload.get("scenario_key"),
            "svg_url": payload.get("svg_url"),
            "stored_svg": payload.get("stored_svg"),
        })
    return jsonify({"runs": items[:50]})


@app.route("/api/runs/<run_id>")
def api_run_detail(run_id):
    path = _run_path(run_id)
    if not os.path.exists(path):
        return jsonify({"error": "任务不存在或已被清理"}), 404
    return send_file(path, mimetype="application/json")


@app.route("/api/protocol")
def api_protocol():
    return jsonify(PROTOCOL_SUBSET)


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="SVG 观测采集器网页服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    # 首次启动自动确保样例存在（不读取真值，仅生成公开 SVG）
    if not os.path.exists(os.path.join(SAMPLES_DIR, "index.json")):
        from build_samples import build
        build(SAMPLES_DIR)

    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
