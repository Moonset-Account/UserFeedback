"""HTTP 端到端：Flask 测试客户端验证 API 与“运行示例”链路（无需浏览器）。"""
import pytest

from mailacq.storage import load_run
from mailacq.webapp import create_app


@pytest.fixture()
def client():
    app = create_app()
    app.testing = True
    return app.test_client()


def test_index_page_chinese_and_demo_button(client):
    html = client.get("/").get_data(as_text=True)
    assert "实验通知只读采集器" in html
    assert 'id="run-demo"' in html
    assert "运行示例" in html


def test_run_demo_creates_two_retained_runs(client):
    resp = client.post("/api/run-demo")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert len(data["results"]) == 2
    assert data["total_ms"] < 15000  # 验收目标：15 秒内
    scenarios = {r["scenario"] for r in data["results"]}
    assert scenarios == {"normal_basic", "bad_attachment"}
    for item in data["results"]:
        detail = load_run(item["run_id"])
        assert detail["scenario"] == item["scenario"]


def test_run_single_scenario_and_detail_api(client):
    resp = client.post("/api/run", json={"scenario": "uid_reset"})
    data = resp.get_json()
    assert data["ok"] is True
    detail = client.get(f"/api/runs/{data['run_id']}").get_json()
    assert detail["identity"]["generation_switches"] == 1
    assert detail["identity"]["reused_uids"][0]["uid"] == 1


def test_run_all_five_scenarios(client):
    data = client.post("/api/run-all").get_json()
    assert len(data["results"]) == 5
    statuses = {r["scenario"]: r["status"] for r in data["results"]}
    # bad_attachment 预期 partial（含附件解码错误），其余 success
    assert statuses["bad_attachment"] == "partial"
    for key, status in statuses.items():
        if key != "bad_attachment":
            assert status == "success"


def test_unknown_scenario_rejected(client):
    resp = client.post("/api/run", json={"scenario": "nope"})
    assert resp.status_code == 400
