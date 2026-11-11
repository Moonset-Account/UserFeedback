"""中文操作入口 Web 应用：配置输入、运行采集任务、查看来源证据与可解释错误。"""
from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, render_template, request

from generator import DATASETS, DEMO_SCENARIOS, SCENARIO_ORDER, SCENARIO_TITLES_CN
from labsoap import collect
from labsoap.errors import ERROR_MESSAGES_CN, SUPPORTED_SUBSET

WEB_DIR = Path(__file__).resolve().parent


def create_app(instrument_base_url: str) -> Flask:
    app = Flask(__name__, template_folder=str(WEB_DIR / "templates"),
                static_folder=str(WEB_DIR / "static"))
    app.config["INSTRUMENT_BASE_URL"] = instrument_base_url.rstrip("/")

    def _source_for(scenario: str, mode: str) -> dict:
        if mode == "file":
            return {"kind": "file", "ref": f"{scenario}.mime"}
        return {"kind": "http", "ref": f"{app.config['INSTRUMENT_BASE_URL']}/soap?scenario={scenario}"}

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            scenarios=SCENARIO_ORDER,
            titles=SCENARIO_TITLES_CN,
            demo=DEMO_SCENARIOS,
            base_url=app.config["INSTRUMENT_BASE_URL"],
            supported_subset=SUPPORTED_SUBSET,
        )

    @app.get("/api/scenarios")
    def api_scenarios():
        return jsonify({
            "scenarios": [
                {"scenario": s, "title": SCENARIO_TITLES_CN[s],
                 "kind": "fault" if "fault" in DATASETS[s] else "measurement"}
                for s in SCENARIO_ORDER
            ],
            "demo_scenarios": DEMO_SCENARIOS,
            "error_catalog": ERROR_MESSAGES_CN,
            "supported_subset": SUPPORTED_SUBSET,
            "instrument_base_url": app.config["INSTRUMENT_BASE_URL"],
        })

    @app.post("/api/run")
    def api_run():
        payload = request.get_json(silent=True) or {}
        scenario = payload.get("scenario", "complete")
        mode = payload.get("mode", "http")
        if scenario not in DATASETS:
            return jsonify({"ok": False, "message": f"未知场景：{scenario}"}), 400
        if mode not in ("http", "file"):
            return jsonify({"ok": False, "message": "mode 仅支持 http 或 file"}), 400
        result = collect(_source_for(scenario, mode))
        result["scenario"] = scenario
        result["scenario_title"] = SCENARIO_TITLES_CN[scenario]
        result["mode"] = mode
        return jsonify({"ok": True, "result": result})

    @app.post("/api/run-batch")
    def api_run_batch():
        payload = request.get_json(silent=True) or {}
        mode = payload.get("mode", "http")
        scenarios = payload.get("scenarios") or SCENARIO_ORDER
        if mode not in ("http", "file"):
            return jsonify({"ok": False, "message": "mode 仅支持 http 或 file"}), 400
        scenarios = [s for s in scenarios if s in DATASETS]
        results = []
        for scenario in scenarios:
            result = collect(_source_for(scenario, mode))
            result["scenario"] = scenario
            result["scenario_title"] = SCENARIO_TITLES_CN[scenario]
            result["mode"] = mode
            results.append(result)
        summary = {
            "total": len(results),
            "complete": sum(1 for r in results if r["status"] == "complete"),
            "fault": sum(1 for r in results if r["status"] == "fault"),
            "rejected": sum(1 for r in results if r["status"] == "rejected"),
            "fetch_error": sum(1 for r in results if r["status"] == "fetch_error"),
        }
        return jsonify({"ok": True, "results": results, "summary": summary})

    @app.get("/api/samples")
    def api_samples():
        samples_dir = Path(__file__).resolve().parent.parent / "samples"
        files = sorted(p.name for p in samples_dir.glob("*.mime")) if samples_dir.is_dir() else []
        return jsonify({"samples_dir": str(samples_dir), "files": files})

    return app
