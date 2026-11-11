"""Web 应用工厂：中文操作界面 + 任务 API。

路由分组：
  /                     中文控制台页面
  /api/scenarios        场景目录（来自生成器的公开清单，不含期望真值）
  /api/jobs             任务创建 / 列表 / 详情 / 复判
  /api/rules            自定义规则读写
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, render_template, request

from classifier import RuleError
from classifier.models import CATEGORY_LABELS_ZH
from runner import JobStore
from source_site.catalog import DEMO_SCENARIO_IDS, public_catalog


def create_app(runtime_dir: str | Path = "runtime") -> Flask:
    base = Path(__file__).resolve().parent
    app = Flask(
        __name__,
        template_folder=str(base / "web" / "templates"),
        static_folder=str(base / "web" / "static"),
    )
    store = JobStore(runtime_dir)
    app.config["JOB_STORE"] = store

    def _source_base() -> str:
        override = app.config.get("SOURCE_BASE_OVERRIDE")
        return override.rstrip("/") if override else request.host_url.rstrip("/")

    # 场景目录只暴露 id/标题/说明/路径，不暴露期望分类真值
    scenario_catalog = public_catalog()

    @app.get("/")
    def console():
        return render_template(
            "console.html",
            categories=CATEGORY_LABELS_ZH,
            demo_ids=DEMO_SCENARIO_IDS,
        )

    @app.get("/api/scenarios")
    def api_scenarios():
        return jsonify({
            "scenarios": scenario_catalog,
            "demo_ids": DEMO_SCENARIO_IDS,
            "full_suite_ids": [s["id"] for s in scenario_catalog],
        })

    @app.post("/api/jobs")
    def api_create_job():
        payload = request.get_json(force=True, silent=True) or {}
        target = (payload.get("target") or "").strip()
        mode = payload.get("mode", "http")
        referer = (payload.get("referer") or "").strip()
        label = (payload.get("label") or "").strip()
        if not target:
            return jsonify({"error": "缺少 target（采集目标 URL 或文件路径）"}), 400
        if mode not in ("http", "file", "browser"):
            return jsonify({"error": "mode 仅支持 http / file / browser"}), 400
        try:
            rules = store.load_current_rules()
            job = store.run(target, mode=mode, referer=referer, label=label, rules=rules)
        except RuleError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"job": store._summary(job), "job_id": job["job_id"]}), 201

    @app.post("/api/suites/demo")
    def api_run_demo():
        """运行示例：正常 + 边界两个小场景，要求 15 秒内完成。"""
        host = _source_base()
        jobs = []
        for sid in DEMO_SCENARIO_IDS:
            spec = next(s for s in scenario_catalog if s["id"] == sid)
            job = store.run(
                host + spec["path"], mode="http",
                label=f"运行示例：{spec['title']}",
                rules=store.load_current_rules(),
            )
            jobs.append(store._summary(job))
        return jsonify({"jobs": jobs, "job_ids": [j["job_id"] for j in jobs]}), 201

    @app.post("/api/suites/full")
    def api_run_full():
        """完整验收：全部 22 个场景（含传输失败）。"""
        host = _source_base()
        jobs = []
        for spec in scenario_catalog:
            target = host + spec["path"]
            try:
                job = store.run(
                    target, mode="http",
                    label=f"完整验收：{spec['title']}",
                    rules=store.load_current_rules(),
                )
            except RuleError as exc:
                return jsonify({"error": str(exc)}), 400
            jobs.append(store._summary(job))
        return jsonify({"jobs": jobs, "job_ids": [j["job_id"] for j in jobs]}), 201

    @app.get("/api/jobs")
    def api_list_jobs():
        return jsonify({"jobs": store.list_jobs()})

    @app.get("/api/jobs/<job_id>")
    def api_job_detail(job_id: str):
        job = store.get_job(job_id)
        if job is None:
            return jsonify({"error": "任务不存在"}), 404
        return jsonify({"job": job})

    @app.post("/api/jobs/<job_id>/reclassify")
    def api_reclassify(job_id: str):
        payload = request.get_json(force=True, silent=True) or {}
        note = (payload.get("note") or "").strip()
        try:
            job = store.reclassify(job_id, note=note)
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404
        except RuleError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"job": job})

    @app.get("/api/rules")
    def api_get_rules():
        path = store.current_rules_path()
        content = path.read_text(encoding="utf-8") if path else "[]"
        return jsonify({"rules_json": content, "exists": path is not None})

    @app.put("/api/rules")
    def api_put_rules():
        payload = request.get_json(force=True, silent=True) or {}
        rules_json = payload.get("rules_json", "")
        try:
            store.save_rules(rules_json)
        except RuleError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    return app
