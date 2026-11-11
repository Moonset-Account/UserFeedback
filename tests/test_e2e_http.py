"""端到端（HTTP 层）：Web API → 真实后台线程 → 真实 HTTP 采集 → 前端消费的数据结构。

不使用 mock：源站为真实 Werkzeug 服务，采集器发出真实 requests 请求。
"""
from __future__ import annotations

import time

import pytest

from webapp.app import create_app


@pytest.fixture()
def client(source_server):
    app = create_app(source_server)
    app.config.update(TESTING=True)
    return app.test_client()


def _wait(client, task_id, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/tasks/{task_id}").get_json()
        if body["state"] in ("done", "error"):
            return body
        time.sleep(0.1)
    raise AssertionError("任务超时未结束")


def test_full_task_lifecycle(client):
    resp = client.post("/api/tasks", json={
        "start_url": "/api/catalog", "seed": 20261111,
        "sample_size": 12, "request_budget": 30, "label": "E2E 正常"})
    assert resp.status_code == 202
    task = _wait(client, resp.get_json()["task_id"])
    assert task["state"] == "done"
    r = task["result"]
    assert r["status"] == "ok" and r["stop_reason"] == "completed"
    assert len(r["sampling"]["sample"]) == 12
    assert r["observed_unique_count"] == 80
    assert r["duplicate_count"] > 0
    # 来源证据完整且指向真实可访问的 URL
    for src in r["sample_sources"]:
        assert src["source_page_url"].startswith("http")
    # 页面静态资源可访问
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_demo_chain_runs_both_scenarios_within_15s(client):
    t0 = time.monotonic()
    resp = client.post("/api/demo")
    assert resp.status_code == 202
    deadline = time.monotonic() + 15
    results = []
    while time.monotonic() < deadline:
        tasks = client.get("/api/tasks").get_json()
        demo = [t for t in tasks if t["label"].startswith("示例")]
        if len(demo) >= 2 and all(t["state"] in ("done", "error") for t in demo):
            results = demo
            break
        time.sleep(0.15)
    elapsed = time.monotonic() - t0
    assert len(results) == 2, "演示链应在 15 秒内完成两个场景"
    assert elapsed < 15, f"演示链耗时 {elapsed:.1f}s 超过 15 秒目标"
    labels = {t["label"] for t in results}
    assert any("正常" in x for x in labels) and any("边界" in x for x in labels)
    by_status = {t["result"]["stop_reason"]: t for t in results}
    assert "completed" in by_status and "budget_exhausted" in by_status
    # 两个结果都保留可查看
    for t in results:
        got = client.get(f"/api/tasks/{t['task_id']}").get_json()
        assert got["result"]["declaration"]


def test_budget_scenario_and_sample_exceeds_scenario(client):
    resp = client.post("/api/scenarios/demo-budget")
    task = _wait(client, resp.get_json()["task_id"])
    assert task["result"]["stop_reason"] == "budget_exhausted"
    assert "全站无偏样本" in task["result"]["declaration"]
    assert "已观察" in task["result"]["declaration"]

    resp = client.post("/api/scenarios/sample-exceeds")
    task = _wait(client, resp.get_json()["task_id"])
    assert task["result"]["sampling"]["full_population_taken"] is True
    assert len(task["result"]["sampling"]["sample"]) == task["result"]["observed_unique_count"]


def test_invalid_params_are_rejected(client):
    resp = client.post("/api/tasks", json={
        "start_url": "/api/catalog", "seed": "x",
        "sample_size": 5, "request_budget": 10})
    assert resp.status_code == 400
    resp = client.post("/api/tasks", json={
        "start_url": "/api/catalog", "seed": 1,
        "sample_size": -1, "request_budget": 10})
    assert resp.status_code == 400


def test_frequency_endpoint(client):
    body = client.post("/api/frequency", json={"trials": 1000}).get_json()
    assert body["passed_at_0_05"] is True
    assert "不构成全站覆盖证明" in body["disclaimer"]


def test_meta_exposes_protocol_subset(client):
    meta = client.get("/api/meta").get_json()
    assert "scenarios" in meta and len(meta["scenarios"]) >= 5
    assert any("JSON" in s for s in meta["protocol_subset"])
