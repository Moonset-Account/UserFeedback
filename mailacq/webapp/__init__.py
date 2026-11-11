"""Flask 网页应用：中文操作入口、任务运行、来源证据与错误展示。"""
from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from ..config import MOCK_PASSWORD, MOCK_USERNAME, SUPPORTED_IMAP_SUBSET, ensure_dirs
from ..generator.generate import generate_all
from ..generator.scenarios import ALL_SCENARIOS, DEMO_SCENARIOS, SCENARIO_BUILDERS
from ..runner import run_scenario
from ..storage import list_runs, load_run, save_run


def create_app() -> Flask:
    ensure_dirs()
    # 首次启动自动生成全部虚构样例（幂等）
    generate_all()

    app = Flask(__name__, template_folder="templates", static_folder="static")

    @app.get("/")
    def index():
        scenario_meta = [
            {"key": key, "title": build.title, "description": build.description}
            for key in ALL_SCENARIOS
            for build in [SCENARIO_BUILDERS[key]()]
        ]
        return render_template(
            "index.html",
            scenarios=scenario_meta,
            demo_scenarios=DEMO_SCENARIOS,
            username=MOCK_USERNAME,
            protocol_subset=SUPPORTED_IMAP_SUBSET,
        )

    @app.get("/api/scenarios")
    def api_scenarios():
        return jsonify([
            {"key": key, "title": SCENARIO_BUILDERS[key]().title,
             "description": SCENARIO_BUILDERS[key]().description}
            for key in ALL_SCENARIOS
        ])

    @app.post("/api/run")
    def api_run():
        payload = request.get_json(force=True, silent=True) or {}
        scenario = payload.get("scenario", "")
        if scenario not in SCENARIO_BUILDERS:
            return jsonify({"ok": False, "error": f"未知场景: {scenario}"}), 400
        record = run_scenario(scenario)
        save_run(record)
        return jsonify({"ok": True, "run_id": record.run_id,
                        "duration_ms": record.duration_ms, "status": record.status})

    @app.post("/api/run-demo")
    def api_run_demo():
        """“运行示例”按钮：小样例，一个正常 + 一个边界，保留两次结果。"""
        results = []
        for scenario in DEMO_SCENARIOS:
            record = run_scenario(scenario)
            save_run(record)
            results.append({
                "scenario": scenario, "run_id": record.run_id,
                "duration_ms": record.duration_ms, "status": record.status,
            })
        total_ms = sum(r["duration_ms"] for r in results)
        return jsonify({"ok": True, "results": results, "total_ms": total_ms})

    @app.post("/api/run-all")
    def api_run_all():
        """完整验收：依次运行全部五个场景。"""
        results = []
        for scenario in ALL_SCENARIOS:
            record = run_scenario(scenario)
            save_run(record)
            results.append({
                "scenario": scenario, "run_id": record.run_id,
                "duration_ms": record.duration_ms, "status": record.status,
            })
        return jsonify({"ok": True, "results": results,
                        "total_ms": sum(r["duration_ms"] for r in results)})

    @app.get("/api/runs")
    def api_runs():
        return jsonify({"runs": list_runs(),
                        "credentials": {"username": MOCK_USERNAME,
                                        "password": MOCK_PASSWORD}})

    @app.get("/api/runs/<run_id>")
    def api_run_detail(run_id: str):
        try:
            return jsonify(load_run(run_id))
        except FileNotFoundError:
            return jsonify({"ok": False, "error": "运行记录不存在"}), 404

    return app
