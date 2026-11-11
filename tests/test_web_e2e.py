"""Web 控制台端到端测试（Flask 测试客户端 + 真实源站）。"""

from __future__ import annotations

import json

import pytest

from webapp import create_app


@pytest.fixture()
def client(runtime_dir, source_server):
    app = create_app(runtime_dir=runtime_dir / "console")
    app.config["TESTING"] = True
    # 让套件 API 拼出的 URL 指向真实源站
    app.config["SOURCE_BASE_OVERRIDE"] = source_server
    with app.test_client() as c:
        c.source_base = source_server
        yield c


def test_console_root_renders_chinese_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "响应语义判定器" in body
    assert 'id="run-demo"' in body          # 验收要求的按钮 id
    assert "运行示例" in body


def test_scenario_catalog_has_no_truth(client):
    """公开场景目录不得泄露期望分类。"""
    resp = client.get("/api/scenarios")
    data = resp.get_json()
    assert len(data["scenarios"]) >= 20
    for s in data["scenarios"]:
        assert "expect" not in s
        assert "expected_category" not in s
    assert set(data["demo_ids"]) == {"normal", "empty-zh"}


def test_create_single_job_real_execution(client):
    resp = client.post("/api/jobs", json={
        "target": client.source_base + "/scenarios/maintenance-zh",
        "mode": "http",
    })
    assert resp.status_code == 201
    job_id = resp.get_json()["job_id"]
    detail = client.get(f"/api/jobs/{job_id}").get_json()["job"]
    latest = detail["history"][-1]
    assert latest["category"] == "MAINTENANCE"
    assert latest["evidences"], "结果必须带证据"


def test_demo_suite_finishes_quickly_and_keeps_both(client):
    import time

    t0 = time.time()
    resp = client.post("/api/suites/demo")
    elapsed = time.time() - t0
    assert resp.status_code == 201
    jobs = resp.get_json()["jobs"]
    assert len(jobs) == 2                     # 一个正常 + 一个边界
    assert elapsed < 15                       # 15 秒内
    cats = {j["latest_category"] for j in jobs}
    assert cats == {"VALID", "EMPTY"}
    # 两个结果都可查看
    for j in jobs:
        detail = client.get(f"/api/jobs/{j['job_id']}")
        assert detail.status_code == 200


def test_full_suite_covers_all_categories(client):
    resp = client.post("/api/suites/full")
    assert resp.status_code == 201
    jobs = resp.get_json()["jobs"]
    cats = {j["latest_category"] for j in jobs}
    # 八大类别全部出现（含 UNKNOWN 隔离与 FETCH_FAILED）
    assert cats == {
        "VALID", "EMPTY", "MAINTENANCE", "ACCESS_NOTICE",
        "ERROR_PAGE", "BROKEN_TEMPLATE", "FETCH_FAILED", "UNKNOWN",
    }


def test_rules_validation_and_reclassify_flow(client):
    # 坏规则 400
    bad = client.put("/api/rules", json={"rules_json": "[] broken"})
    assert bad.status_code == 400

    # 先造一个隔离任务
    created = client.post("/api/jobs", json={
        "target": client.source_base + "/scenarios/uncertain-busy",
        "mode": "http",
    }).get_json()
    job_id = created["job_id"]
    before = client.get(f"/api/jobs/{job_id}").get_json()["job"]
    assert before["history"][-1]["category"] == "UNKNOWN"

    # 保存好规则并复判
    saved = client.put("/api/rules", json={"rules_json": json.dumps([{
        "code": "rule-busy-001",
        "name": "忙碌页补充识别",
        "phrase": "正在为您检索相关资料",
        "region_contains": "pending-tip",
        "category": "BROKEN_TEMPLATE",
        "weight": 0.6,
    }], ensure_ascii=False)})
    assert saved.status_code == 200

    rejudged = client.post(f"/api/jobs/{job_id}/reclassify",
                           json={"note": "页面操作复判"}).get_json()["job"]
    history = rejudged["history"]
    assert len(history) == 2
    assert history[0]["category"] == "UNKNOWN"
    assert history[1]["category"] == "BROKEN_TEMPLATE"


def test_job_listing_and_404(client):
    assert client.get("/api/jobs").status_code == 200
    assert client.get("/api/jobs/nonexistent-id").status_code == 404


def test_bad_input_rejected(client):
    resp = client.post("/api/jobs", json={"target": "", "mode": "http"})
    assert resp.status_code == 400
    resp = client.post("/api/jobs", json={"target": "x", "mode": "ftp"})
    assert resp.status_code == 400
