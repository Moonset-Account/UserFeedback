# -*- coding: utf-8 -*-
"""操作台 Flask 应用工厂与 JSON API。

页面入口 / ；所有任务结果来自 collector 真实执行，
后端不预填任何成功结果。运行记录持久化到 runs/ 目录。
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

from flask import Flask, jsonify, render_template, request, send_from_directory

from collector import tasks
from collector.locales import locale_choices, RULES
from generator import sample_data
from .source_site import source_bp


def create_app(samples_dir: str, runs_dir: str) -> Flask:
    app = Flask(__name__,
                template_folder=os.path.join(os.path.dirname(__file__), "templates"),
                static_folder=os.path.join(os.path.dirname(__file__), "static"))
    app.config["SAMPLES_DIR"] = samples_dir
    app.config["RUNS_DIR"] = runs_dir
    os.makedirs(runs_dir, exist_ok=True)

    app.register_blueprint(source_bp)

    # 启动即确保虚构样例存在（幂等）
    sample_data.generate(samples_dir)

    def save_run(payload: dict) -> str:
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        payload["run_id"] = run_id
        payload["saved_at"] = datetime.now().isoformat(timespec="seconds")
        with open(os.path.join(runs_dir, f"{run_id}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return run_id

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/locales")
    def api_locales():
        return jsonify({
            "choices": locale_choices(),
            "descriptions": {c: r.describe() for c, r in RULES.items()},
        })

    @app.get("/api/samples")
    def api_samples():
        src = os.path.join(samples_dir, "source")
        files = [
            {"name": n, "path": os.path.join(src, n)}
            for n in sorted(os.listdir(src))
        ] if os.path.isdir(src) else []
        return jsonify({
            "files": files,
            "source_urls": {
                "html": "/source/measurements.html",
                "json": "/source/measurements.json",
                "csv": "/source/measurements.csv",
                "de_json": "/source/de/measurements.json",
            },
            "dataset": sample_data.DATASET_ID,
        })

    @app.post("/api/run")
    def api_run():
        body = request.get_json(force=True, silent=True) or {}
        mode = body.get("mode", "text")
        locale = body.get("locale") or "und"
        if locale not in RULES:
            return jsonify({"error": f"未知地区规则 {locale!r}"}), 400
        try:
            if mode == "text":
                lines = [ln for ln in body.get("text", "").splitlines() if ln.strip()]
                if not lines:
                    return jsonify({"error": "输入为空：请粘贴至少一行测量原文"}), 400
                result = tasks.run_text_input(lines, locale)
            elif mode == "file":
                path = body.get("path", "").strip()
                if not path:
                    return jsonify({"error": "缺少文件路径 path"}), 400
                result = tasks.run_file(
                    path, None if locale == "und" else locale,
                    value_field=body.get("value_field", "value"),
                    id_field=body.get("id_field", "id"),
                )
            elif mode == "http":
                url = body.get("url", "").strip()
                if not url:
                    return jsonify({"error": "缺少接口地址 url"}), 400
                result = tasks.run_http(
                    url, None if locale == "und" else locale,
                    value_field=body.get("value_field", "value"),
                    id_field=body.get("id_field", "id"),
                )
            elif mode == "browser":
                url = body.get("url", "").strip()
                if not url:
                    return jsonify({"error": "缺少页面地址 url"}), 400
                result = tasks.run_browser(
                    url, None if locale == "und" else locale,
                    headless=bool(body.get("headless", True)),
                )
            else:
                return jsonify({"error": f"未知采集模式 {mode!r}"}), 400
        except Exception as exc:  # 兜底：错误也要结构化返回给页面
            return jsonify({"error": f"任务异常: {exc}"}), 500

        payload = {"mode": mode, "request_locale": locale, "result": result.to_dict()}
        payload["run_id"] = save_run(payload)
        return jsonify(payload)

    @app.post("/api/demo")
    def api_demo():
        """运行示例按钮：小样例依次跑一个正常场景和一个边界场景。"""
        normal = tasks.run_text_input(sample_data.DEMO_NORMAL, "en_US")
        boundary = tasks.run_text_input(sample_data.DEMO_BOUNDARY, "und")
        payload = {
            "mode": "demo",
            "scenarios": [
                {"name": "正常场景（en_US：分组、科学计数法、对称不确定度）",
                 "result": normal.to_dict()},
                {"name": "边界场景（und：缺失值、检测限、非法单位、歧义）",
                 "result": boundary.to_dict()},
            ],
        }
        payload["run_id"] = save_run(payload)
        return jsonify(payload)

    @app.post("/api/acceptance")
    def api_acceptance():
        """完整验收场景：5 种地区规则 × 歧义原文 + 多格式主数据集。"""
        runs = []
        # 1) 同一批歧义原文在 5 种规则下重新解释
        for loc in sample_data.ACCEPTANCE_LOCALES:
            runs.append({
                "scenario": f"歧义原文重解释 / {loc}",
                "result": tasks.reinterpret(sample_data.AMBIGUITY_ROWS, loc).to_dict(),
            })
        # 2) 主数据集（文件接口采集，源声明 en_US）
        en_path = os.path.join(samples_dir, "source", "measurements_en.json")
        runs.append({
            "scenario": "主数据集 / 文件接口 / 源声明 en_US",
            "result": tasks.run_file(en_path, None).to_dict(),
        })
        # 3) 德语数据集（源声明 de_DE）
        de_path = os.path.join(samples_dir, "source", "measurements_de.json")
        runs.append({
            "scenario": "德语子集 / 文件接口 / 源声明 de_DE",
            "result": tasks.run_file(de_path, None).to_dict(),
        })
        # 4) 未声明地区 TXT（und，歧义必须报错）
        txt_path = os.path.join(samples_dir, "source", "measurements_undeclared.txt")
        runs.append({
            "scenario": "无地区声明 TXT / und 规则",
            "result": tasks.run_file(txt_path, None).to_dict(),
        })
        payload = {"mode": "acceptance", "runs": runs}
        payload["run_id"] = save_run(payload)
        return jsonify(payload)

    @app.get("/api/runs")
    def api_runs():
        names = sorted(
            (n for n in os.listdir(runs_dir) if n.endswith(".json")),
            reverse=True,
        )
        return jsonify({"runs": names})

    @app.get("/api/runs/<name>")
    def api_run_detail(name):
        if "/" in name or ".." in name or not name.endswith(".json"):
            return jsonify({"error": "非法文件名"}), 400
        return send_from_directory(runs_dir, name)

    return app
