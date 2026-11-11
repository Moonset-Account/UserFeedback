"""Flask 应用工厂：挂载源站 API、Web 内部 API 与中文操作页面。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from flask import Flask, render_template

from collector.loader import load_spec
from server.origin_api import api_bp
from server.origin_filters import filter_experiments
from server.web_api import web_bp

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "openapi" / "experiments.openapi.yaml"
DATA_PATH = ROOT / "data" / "experiments.json"
SCENARIOS_PATH = ROOT / "fixtures" / "scenarios.json"


def _ensure_data() -> None:
    """数据缺失时调用独立生成器产出（仅本地虚构数据）。"""
    if DATA_PATH.exists():
        return
    subprocess.run(
        [sys.executable, str(ROOT / "generator" / "generate.py"),
         "--out", str(ROOT / "data")],
        check=True, cwd=str(ROOT),
    )


def create_app(config: dict | None = None) -> Flask:
    _ensure_data()
    app = Flask(__name__, template_folder=str(ROOT / "web" / "templates"),
                static_folder=str(ROOT / "web" / "static"))
    spec = load_spec(SPEC_PATH)
    experiments = json.loads(DATA_PATH.read_text(encoding="utf-8"))

    app.config.update(
        SPEC=spec,
        EXPERIMENTS=experiments,
        ORIGIN_FILTER=filter_experiments,
        SCENARIOS_PATH=str(SCENARIOS_PATH),
        BASE_URL="http://127.0.0.1",  # 端口在 run() 时按实际端口覆盖
    )
    if config:
        app.config.update(config)

    app.register_blueprint(api_bp)
    app.register_blueprint(web_bp)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "rows": len(experiments)}

    return app
