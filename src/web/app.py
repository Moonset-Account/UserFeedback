"""Flask 应用工厂：源站接口 + 采集控制 API + 中文操作页面。"""
from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, render_template

from .api import api_bp
from .source import source_bp
from .task_manager import TaskManager


def create_app(recordings_dir: str | Path | None = None) -> Flask:
    project_root = Path(__file__).resolve().parents[2]
    static_dir = project_root / "src" / "web" / "static"
    template_dir = project_root / "src" / "web" / "templates"

    app = Flask(
        __name__,
        static_folder=str(static_dir),
        template_folder=str(template_dir),
    )
    recordings = Path(recordings_dir or (project_root / "runtime" / "recordings"))
    recordings.mkdir(parents=True, exist_ok=True)

    # 源站地址仅用于任务管理器拼 HTTP 采集 URL；默认本机回环。
    host = os.environ.get("RIP_HOST", "127.0.0.1")
    port = os.environ.get("RIP_PORT", "5000")
    app.config["TASK_MANAGER"] = TaskManager(
        recordings_dir=str(recordings),
        source_base_url=f"http://{host}:{port}",
    )

    app.register_blueprint(source_bp)
    app.register_blueprint(api_bp)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "protocol": "RIP/1.0"}

    return app
