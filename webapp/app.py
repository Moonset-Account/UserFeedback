"""Flask Web 应用：中文操作入口 + 本地样例源站托管。"""
from __future__ import annotations

import json
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from iiif_collector import collect
from sample_site.generator import SCENARIOS, generate_site

from .run_store import RunStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "webapp" / "static"
DATA_DIR = PROJECT_ROOT / "data"
SAMPLE_DIR = DATA_DIR / "sample"


def create_app(host: str = "127.0.0.1", port: int = 5000) -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    base_url = f"http://{host}:{port}/sample"
    site_meta = generate_site(SAMPLE_DIR, base_url)
    store = RunStore(DATA_DIR)
    app.config["SITE_META"] = site_meta
    app.config["RUN_STORE"] = store

    # ---------------- 页面 ---------------- #
    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    # ---------------- 本地样例源站（公开静态接口） ---------------- #
    @app.get("/sample/<path:relpath>")
    def sample_files(relpath: str):
        return send_from_directory(SAMPLE_DIR, relpath)

    @app.get("/api/sources")
    def sources():
        return jsonify(
            {
                "note": "以下样例全部为虚构数据，由 sample_site/generator.py 生成",
                "base_url": base_url,
                "scenarios": [
                    {
                        "slug": slug,
                        "title": meta["title"],
                        "manifest_url": meta["manifest_url"],
                        "image_count": meta["image_count"],
                    }
                    for slug, meta in site_meta["scenarios"].items()
                ],
                "supported_subset": [
                    "Manifest（@context/id/type/label/items/structures）",
                    "Canvas 与 AnnotationPage",
                    "painting Annotation，body 为 Image",
                    "Choice 资源（采集全部候选，首个为默认）",
                    "Range 层级结构（Canvas/SpecificResource/Range 引用）",
                    "ImageService3 描述记录（不做动态图像编辑）",
                ],
            }
        )

    # ---------------- 采集任务 ---------------- #
    @app.post("/api/run")
    def run_collect():
        payload = request.get_json(silent=True) or {}
        source = (payload.get("source") or "").strip()
        scenario = payload.get("scenario")
        if not source:
            return jsonify({"error": "缺少 source 参数（清单 URL 或本地文件路径）"}), 400
        if len(source) > 2000:
            return jsonify({"error": "source 过长"}), 400

        result = collect(source).to_dict(include_raw=False)
        run = store.create(source=source, scenario=scenario, result=result)
        return jsonify(run)

    @app.post("/api/demo")
    def run_demo():
        """依次运行正常场景与边界场景，保留两次结果，目标 15 秒内完成。"""
        runs = []
        for slug in ("demo-normal", "demo-boundary"):
            manifest_url = site_meta["scenarios"][slug]["manifest_url"]
            result = collect(manifest_url).to_dict(include_raw=False)
            runs.append(store.create(source=manifest_url, scenario=slug, result=result))
        return jsonify({"runs": runs})

    @app.get("/api/runs")
    def list_runs():
        return jsonify({"runs": store.list_runs()})

    @app.get("/api/runs/<run_id>")
    def get_run(run_id: str):
        run = store.get(run_id)
        if run is None:
            return jsonify({"error": "运行记录不存在"}), 404
        return jsonify(run)

    @app.delete("/api/runs")
    def clear_runs():
        store.clear()
        return jsonify({"ok": True})

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "scenarios": list(SCENARIOS.keys())})

    return app
