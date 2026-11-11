"""Web 入口：python3 app.py --host 127.0.0.1 --port <端口>

启动后：
  1. 自动生成样例源站页面与独立真值（generated/，虚构数据）；
  2. 启动 Flask 服务，根路径为中文操作界面；
  3. 采集器只通过浏览器访问本服务公开的 /samples/ 页面或用户填写的 URL。

无需手动激活虚拟环境：本文件把项目根目录加入 sys.path，
并在 requirements 缺失时给出明确安装提示。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from flask import Flask, jsonify, render_template, request, send_from_directory, abort
except ImportError:  # pragma: no cover
    print("缺少依赖 Flask，请先执行：python3 -m pip install -r requirements.txt", file=sys.stderr)
    raise

from collector.browser import BrowserDriver
from collector.engine import Engine
from collector.storage import Storage
from site_gen.generator import SCENARIOS, SCENARIO_TITLES, generate_all

GENERATED_DIR = Path(os.environ.get("SAMPLE_GEN_DIR", ROOT / "generated"))
SAMPLES_DIR = GENERATED_DIR / "samples"
DATA_DIR = Path(os.environ.get("COLLECTOR_DATA_DIR", ROOT / "data"))

SCENARIO_DESCRIPTIONS = {
    "baseline": "正常基线：标签、单位（g/%）、限定说明（平均）、数值类型全部符合 v1 契约",
    "restyle": "纯样式改版：仅 CSS 与装饰文案变化，选择器与语义完全不变（不得误报）",
    "label": "标签含义改变：干重→湿重，CSS 选择器保持不变",
    "unit": "单位倍数改变：g→mg，数值按 1000 倍改写，选择器保持不变",
    "no-qual": "缺少必要限定说明：列头去掉“平均”，选择器保持不变",
    "median": "统计口径改变：平均→中位数，选择器保持不变",
    "type-error": "数据类型组成改变：某行数值单元格变成文本“仪器离线”",
}


def create_app() -> Flask:
    app = Flask(__name__, template_folder=str(ROOT / "templates"),
                static_folder=str(ROOT / "static"))

    storage = Storage(DATA_DIR)
    driver = BrowserDriver()
    engine = Engine(storage, driver=driver)

    # 启动即生成样例（幂等），保证打开根路径即可运行
    generate_all(GENERATED_DIR)

    def base_url() -> str:
        # 允许测试/反向代理场景显式声明外部可达基址；默认取当前请求的 host
        override = os.environ.get("COLLECTOR_BASE_URL")
        return override.rstrip("/") if override else f"http://{request.host}"

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.get("/samples/<scenario>/index.html")
    def sample_page(scenario: str) -> Any:
        if scenario not in SCENARIOS:
            abort(404)
        return send_from_directory(SAMPLES_DIR / scenario, "index.html")

    @app.get("/api/scenarios")
    def api_scenarios() -> Any:
        return jsonify([
            {
                "key": s,
                "title": SCENARIO_TITLES[s],
                "description": SCENARIO_DESCRIPTIONS[s],
                "url": f"/samples/{s}/index.html",
            }
            for s in SCENARIOS
        ])

    @app.post("/api/runs")
    def api_create_run() -> Any:
        payload = request.get_json(force=True)
        scenario = payload.get("scenario", "custom")
        url = payload.get("url") or ""
        if not url:
            if scenario not in SCENARIOS:
                return jsonify({"error": "未知场景且未提供 url"}), 400
            url = f"{base_url()}/samples/{scenario}/index.html"
        elif url.startswith("/"):
            url = f"{base_url()}{url}"
        try:
            result = engine.run(
                scenario=scenario,
                url=url,
                contract_version=payload.get("contract_version", "v1"),
                note=payload.get("note", ""),
            )
        except Exception as exc:  # 浏览器/导航失败也要如实呈现
            return jsonify({"error": f"采集失败：{type(exc).__name__}: {exc}"}), 502
        return jsonify(_run_summary(result))

    @app.get("/api/runs")
    def api_list_runs() -> Any:
        return jsonify(storage.list_runs())

    @app.get("/api/runs/<run_id>")
    def api_get_run(run_id: str) -> Any:
        try:
            return jsonify(storage.load_run(run_id))
        except FileNotFoundError:
            return jsonify({"error": "运行记录不存在"}), 404

    @app.get("/api/runs/<run_id>/screenshot.png")
    def api_screenshot(run_id: str) -> Any:
        return send_from_directory(storage.runs_dir / run_id, "screenshot.png")

    @app.get("/api/runs/<run_id>/table.html")
    def api_table_html(run_id: str) -> Any:
        return send_from_directory(storage.runs_dir / run_id, "table.html")

    @app.get("/api/runs/<run_id>/suggestions")
    def api_suggestions(run_id: str) -> Any:
        try:
            result = storage.load_run(run_id)
            contract = storage.load_contract(result["contract_version"])
            suggestions = Engine.build_mapping_suggestion(contract, result)
            return jsonify(suggestions)
        except FileNotFoundError:
            return jsonify({"error": "运行记录不存在"}), 404

    @app.post("/api/runs/<run_id>/approve")
    def api_approve(run_id: str) -> Any:
        payload = request.get_json(force=True)
        field_keys = payload.get("field_keys") or []
        if not field_keys:
            return jsonify({"error": "请至少选择一个待批准字段"}), 400
        try:
            outcome = engine.approve(
                run_id=run_id,
                field_keys=field_keys,
                approved_by=payload.get("approved_by", ""),
                reason=payload.get("reason", ""),
                overrides=payload.get("overrides"),
            )
        except FileNotFoundError:
            return jsonify({"error": "运行记录不存在"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(outcome)

    @app.get("/api/contracts")
    def api_contracts() -> Any:
        return jsonify(storage.list_contract_versions())

    @app.get("/api/approvals")
    def api_approvals() -> Any:
        return jsonify(storage.list_approvals())

    @app.post("/api/demo")
    def api_demo() -> Any:
        """运行示例：依次跑一个正常场景（baseline）和一个边界场景（label）。"""
        outcomes = []
        for scenario in ("baseline", "label"):
            url = f"{base_url()}/samples/{scenario}/index.html"
            try:
                result = engine.run(scenario=f"demo-{scenario}", url=url,
                                    contract_version="v1",
                                    note="运行示例按钮触发的小样例")
            except Exception as exc:
                return jsonify({"error": f"场景 {scenario} 采集失败：{exc}"}), 502
            outcomes.append(_run_summary(result))
        return jsonify({"demo_runs": outcomes})

    @app.post("/api/regenerate")
    def api_regenerate() -> Any:
        items = generate_all(GENERATED_DIR)
        return jsonify({"generated": [i.scenario for i in items]})

    @app.get("/api/health")
    def api_health() -> Any:
        return jsonify({"status": "ok"})

    return app, engine


def _run_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": result["run_id"],
        "scenario": result["scenario"],
        "status": result["status"],
        "contract_version": result["contract_version"],
        "violation_codes": sorted({v["code"] for v in result["violations"]}),
        "published_count": len(result["published"]),
        "quarantined_count": len(result["quarantined"]),
        "url": result["url"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="科研测量页面语义契约采集器 Web 入口")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    app, engine = create_app()
    try:
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    finally:
        engine.close()


if __name__ == "__main__":
    main()
