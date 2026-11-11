"""端到端测试：完整 Web 应用（源站 + 网页 API + 真实采集线程）。"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import requests
from flask import Flask
from werkzeug.serving import make_server

from generator.scenarios import DEMO_SCENARIOS
from webapp.server import bp as web_bp


@pytest.fixture(scope="module")
def web_server(source_server, tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("web_runtime")
    app = Flask(__name__, template_folder=Path("webapp/templates").resolve(),
                static_folder=Path("webapp/static").resolve())
    app.config["COLLECTOR_CONFIG"] = {
        "source_base_url": source_server,
        "data_dir": str(data_dir),
        "default_max_refreshes": 3,
    }
    app.register_blueprint(web_bp)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}"
    server.shutdown()


def _wait_finished(base: str, run_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = requests.get(f"{base}/api/runs/{run_id}", timeout=5)
        if resp.status_code == 404:
            time.sleep(0.2)
            continue
        run = resp.json()
        if run["status"] != "running":
            return run
        time.sleep(0.3)
    raise AssertionError("任务超时未结束")


def test_index_page_contains_run_demo_button(web_server):
    html = requests.get(web_server + "/", timeout=5).text
    assert 'id="run-demo"' in html
    assert "运行示例" in html
    assert "公开实验导出链接采集器" in html


def test_full_run_normal_scenario_via_api(web_server):
    resp = requests.post(web_server + "/api/runs", json={
        "scenario_id": "normal", "backend": "http", "reset_first": True,
    }, timeout=5)
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    run = _wait_finished(web_server, run_id)
    assert run["status"] == "success"
    assert run["attempts"] == 1
    assert run["saved_path"] and Path(run["saved_path"]).exists()
    # 证据里必须包含真实页面来源
    acquired = next(e for e in run["events"] if e["kind"] == "link_acquired")
    assert acquired["evidence"]["page_url"].endswith("/experiment/normal")


def test_full_run_version_change_scenario_reports_stop(web_server):
    resp = requests.post(web_server + "/api/runs", json={
        "scenario_id": "version_change", "backend": "http", "reset_first": True,
    }, timeout=5)
    run = _wait_finished(web_server, resp.json()["run_id"])
    assert run["status"] == "failed_version_changed"
    assert "业务版本变化" in run["message"]


def test_demo_batch_runs_both_scenarios_within_time_budget(web_server):
    start = time.time()
    resp = requests.post(web_server + "/api/demo", timeout=5)
    job_id = resp.json()["job_id"]
    job = _wait_finished(web_server, job_id, timeout=20.0)
    elapsed = time.time() - start

    assert job["status"] == "completed"
    assert elapsed < 15.0, f"示例批次耗时 {elapsed:.1f}s 超过 15 秒预算"
    for child_id in job["child_run_ids"]:
        child = requests.get(f"{web_server}/api/runs/{child_id}", timeout=5).json()
        assert child["status"] in ("success", "failed_expired", "failed_version_changed",
                                   "failed_identity", "failed_structure")
    child_scenarios = [
        requests.get(f"{web_server}/api/runs/{cid}", timeout=5).json()["scenario_id"]
        for cid in job["child_run_ids"]
    ]
    assert child_scenarios == DEMO_SCENARIOS


def test_unknown_scenario_rejected(web_server):
    resp = requests.post(web_server + "/api/runs", json={"scenario_id": "nope"}, timeout=5)
    assert resp.status_code == 400
