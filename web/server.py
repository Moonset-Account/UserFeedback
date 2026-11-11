"""Flask 服务：控制台 + 本地源站（流程编辑页）+ JSON API。"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from collector.collector import collect_from_file
from generator.sample_generator import SAMPLES_DIR, build_samples
from rpa import runner

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INITIAL_DIR = SAMPLES_DIR / "initial"


def _scenario_payload(scenario_id: str) -> dict:
    spec = collect_from_file(SAMPLES_DIR / f"{scenario_id}.json")
    initial_file = INITIAL_DIR / f"{scenario_id}.json"
    initial = json.loads(initial_file.read_text(encoding="utf-8")) if initial_file.is_file() \
        else spec.step_ids
    return {
        "spec": json.loads((SAMPLES_DIR / f"{scenario_id}.json").read_text(encoding="utf-8")),
        "initial_order": initial,
    }


def create_app(headless: bool = True) -> Flask:
    build_samples()  # 幂等保证样例存在
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["HEADLESS"] = headless
    app.config["BASE_URL"] = None  # 首请求时按 request.host 填充

    # ------------------------------------------------------------------ #
    # 页面
    # ------------------------------------------------------------------ #
    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/source/<scenario_id>")
    def source_page(scenario_id: str):
        """本地“源站”流程编辑页：固定锚点 + 可拖动卡片，公开 DOM 结构。"""
        data = _scenario_payload(scenario_id)
        spec = data["spec"]
        order = data["initial_order"]
        steps_by_id = {s["id"]: s for s in spec["steps"]}
        fixed = (spec.get("constraints") or {}).get("fixed", {}) or {}
        cards = []
        for sid in order:
            cards.append({
                "id": sid,
                "name": steps_by_id[sid]["name"],
                "group": steps_by_id[sid].get("group") or "",
                "fixed": sid in fixed,
                "fixed_slot": fixed.get(sid),
            })
        return render_template(
            "source.html",
            scenario=spec,
            cards=cards,
            fixed=fixed,
            edges=_edge_list(spec),
        )

    # ------------------------------------------------------------------ #
    # 公开接口（采集器的 HTTP 通道即消费这些接口）
    # ------------------------------------------------------------------ #
    @app.get("/api/scenarios")
    def api_scenarios():
        return jsonify(runner.list_scenarios())

    @app.get("/api/scenario/<scenario_id>")
    def api_scenario(scenario_id: str):
        data = _scenario_payload(scenario_id)
        return jsonify(data["spec"])

    @app.get("/api/scenario/<scenario_id>/initial")
    def api_initial(scenario_id: str):
        return jsonify(_scenario_payload(scenario_id)["initial_order"])

    @app.get("/api/runs")
    def api_runs():
        return jsonify(runner.list_runs())

    @app.get("/api/runs/<run_id>")
    def api_run(run_id: str):
        record = runner.get_run(run_id)
        if not record:
            return jsonify({"error": "run not found"}), 404
        return jsonify(record)

    @app.post("/api/run/<scenario_id>")
    def api_run_scenario(scenario_id: str):
        mode = (request.json or {}).get("mode", "browser")
        base_url = _base_url()
        record = runner.run_scenario(
            scenario_id, base_url=base_url,
            headless=app.config["HEADLESS"], mode=mode,
        )
        return jsonify(record)

    @app.post("/api/demo")
    def api_demo():
        base_url = _base_url()
        # 后台线程运行，前端轮询；保证两个小场景 15 秒内结束
        holder: dict = {}

        def worker() -> None:
            holder["result"] = runner.run_demo_pair(
                base_url, headless=app.config["HEADLESS"]
            )

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=20)
        if "result" not in holder:
            return jsonify({"error": "demo 超时（>20s）"}), 504
        return jsonify(holder["result"])

    @app.post("/api/run-custom")
    def api_run_custom():
        """接收用户在页面配置的 rpa-order/v1 JSON，落为临时场景后运行。"""
        body = request.json or {}
        payload = body.get("spec")
        mode = body.get("mode", "browser")
        initial_order = body.get("initial_order")
        if not isinstance(payload, dict):
            return jsonify({"error": "请求体需要 spec 对象（rpa-order/v1 JSON）"}), 400
        try:
            # 先经协议解析器校验，非法输入直接带具体错误返回
            from core.parser import parse_obj
            spec = parse_obj(payload)
        except Exception as exc:  # ProtocolError
            return jsonify({"error": f"协议校验失败：{exc}"}), 400
        scenario_id = spec.scenario_id
        SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
        (SAMPLES_DIR / f"{scenario_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if isinstance(initial_order, list) and initial_order:
            INITIAL_DIR.mkdir(parents=True, exist_ok=True)
            (INITIAL_DIR / f"{scenario_id}.json").write_text(
                json.dumps(initial_order, ensure_ascii=False), encoding="utf-8"
            )
        base_url = _base_url()
        record = runner.run_scenario(
            scenario_id, base_url=base_url,
            headless=app.config["HEADLESS"], mode=mode,
        )
        return jsonify(record)

    def _base_url() -> str:
        if app.config["BASE_URL"]:
            return app.config["BASE_URL"]
        return f"http://{request.host}"

    return app


def _edge_list(spec: dict) -> list[dict]:
    """把 depends_on 转成 [from, to] 边列表供前端画依赖图。"""
    deps = (spec.get("constraints") or {}).get("depends_on", {}) or {}
    edges = []
    for vid, prereqs in deps.items():
        for uid in prereqs:
            edges.append({"from": uid, "to": vid})
    # 同组边（parser 会展开，前端图上也展示）
    group_last: dict[str, str] = {}
    for s in spec["steps"]:
        g = s.get("group")
        if g:
            if g in group_last:
                edges.append({"from": group_last[g], "to": s["id"], "group_edge": True})
            group_last[g] = s["id"]
    return edges
