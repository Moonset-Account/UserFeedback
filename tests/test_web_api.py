"""Web API 测试：Flask 测试客户端验证运行/批量/场景目录接口的真实执行结果。"""
from __future__ import annotations

import time

import pytest

from web.app import create_app
from tests.expected import expected_values as expected


@pytest.fixture()
def client(instrument_server):
    app = create_app(instrument_server)
    app.config["TESTING"] = True
    return app.test_client()


def test_index_page_chinese_and_demo_button(client):
    html = client.get("/").get_data(as_text=True)
    assert "实验仪器" in html
    assert 'id="run-demo"' in html
    assert "运行示例" in html


def test_scenarios_catalog(client):
    data = client.get("/api/scenarios").get_json()
    names = [s["scenario"] for s in data["scenarios"]]
    assert names == ["complete", "fault", "missing_reference", "duplicate_cid",
                     "parts_reordered", "digest_mismatch"]
    assert data["demo_scenarios"] == ["complete", "duplicate_cid"]


def test_run_single_complete_is_real(client):
    resp = client.post("/api/run", json={"scenario": "complete", "mode": "http"}).get_json()
    assert resp["ok"] is True
    result = resp["result"]
    assert result["status"] == "complete"
    assert len(result["measurement_groups"]) == 2
    # 证据来自真实响应
    assert result["raw_evidence"]["raw_sha256"]
    assert result["raw_evidence"]["http_status"] == 200


def test_run_batch_acceptance_mix(client):
    resp = client.post("/api/run-batch", json={"mode": "http"}).get_json()
    assert resp["ok"] is True
    summary = resp["summary"]
    assert summary["total"] == 6
    assert summary["complete"] == 2          # complete + parts_reordered
    assert summary["fault"] == 1
    assert summary["rejected"] == 3
    by_scenario = {r["scenario"]: r for r in resp["results"]}
    for scenario, want in expected.EXPECTED_STATUS.items():
        assert by_scenario[scenario]["status"] == want


def test_demo_budget_under_15_seconds(client):
    start = time.perf_counter()
    resp = client.post("/api/run-batch",
                       json={"mode": "http", "scenarios": ["complete", "duplicate_cid"]}).get_json()
    elapsed = time.perf_counter() - start
    assert resp["ok"] is True
    assert len(resp["results"]) == 2
    assert resp["results"][0]["status"] == "complete"
    assert resp["results"][1]["status"] == "rejected"
    assert elapsed < expected.DEMO_BUDGET_SECONDS


def test_file_mode_batch(client):
    resp = client.post("/api/run-batch", json={"mode": "file"}).get_json()
    assert resp["ok"] is True
    by_scenario = {r["scenario"]: r for r in resp["results"]}
    assert by_scenario["complete"]["status"] == "complete"
    assert by_scenario["fault"]["status"] == "fault"
    assert by_scenario["duplicate_cid"]["error"]["code"] == "DUPLICATE_CONTENT_ID"


def test_unknown_scenario_rejected(client):
    resp = client.post("/api/run", json={"scenario": "nope", "mode": "http"})
    assert resp.status_code == 400
