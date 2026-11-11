"""Web API 测试：任务调度、上传、原始记录取证（含隔离子进程）。"""
from __future__ import annotations

import io
import json
import time

import pytest

from warc_tool.samplegen import build_demo_normal
from warc_tool.web_app import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 上传与样例目录隔离到临时路径
    monkeypatch.chdir(tmp_path)
    import warc_tool.web_app as web_app
    monkeypatch.setattr(web_app, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(web_app, "SAMPLE_DIR", tmp_path / "samples")
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _wait_job(client, job_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/api/jobs/{job_id}")
        data = resp.get_json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(0.2)
    pytest.fail(f"任务 {job_id} 超时未结束")


def test_health_and_index(client):
    assert client.get("/api/health").get_json()["status"] == "ok"
    page = client.get("/")
    assert page.status_code == 200
    assert "WARC".encode() in page.data


def test_demo_runs_two_jobs_and_keeps_both(client):
    resp = client.post("/api/demo")
    jobs = resp.get_json()["jobs"]
    assert len(jobs) == 2
    results = [_wait_job(client, j["job_id"]) for j in jobs]
    assert all(j["status"] == "done" for j in results)
    labels = [j["label"] for j in results]
    assert any("正常" in x for x in labels)
    assert any("边界" in x for x in labels)
    # 两个结果都保留在队列里
    listing = client.get("/api/jobs").get_json()["jobs"]
    assert len(listing) == 2


def test_demo_completes_quickly(client):
    resp = client.post("/api/demo")
    jobs = resp.get_json()["jobs"]
    started = time.time()
    for j in jobs:
        data = _wait_job(client, j["job_id"])
        assert data["status"] == "done"
    assert time.time() - started < 15  # 验收要求：15 秒内结束


def test_upload_and_extract(client):
    sample = build_demo_normal(str(client.application.config.get("UPLOAD_DIR") or "."))
    with open(sample.path, "rb") as fh:
        resp = client.post("/api/upload", data={"warc": (fh, "demo-normal.warc.gz")},
                           content_type="multipart/form-data")
    job_id = resp.get_json()["job_id"]
    data = _wait_job(client, job_id)
    assert data["status"] == "done"
    result = data["result"]
    assert result["total_records"] == 4
    revisit = next(e for e in result["entries"] if e["rec_type"] == "revisit")
    assert revisit["reused_from_index"] == 1


def test_upload_rejects_bad_suffix(client):
    resp = client.post("/api/upload", data={"warc": (io.BytesIO(b"x"), "evil.exe")},
                       content_type="multipart/form-data")
    assert resp.status_code == 400


def test_raw_record_endpoint_serves_evidence(client):
    sample = build_demo_normal(str(client.application.config.get("UPLOAD_DIR") or "."))
    with open(sample.path, "rb") as fh:
        job_id = client.post("/api/upload",
                             data={"warc": (fh, "demo-normal.warc.gz")},
                             content_type="multipart/form-data").get_json()["job_id"]
    _wait_job(client, job_id)
    payload = client.get(f"/api/jobs/{job_id}/records/1").get_json()
    assert payload["summary"]["rec_type"] == "response"
    assert payload["http_statusline"].startswith("200")
    assert any(k == "Content-Type" for k, _ in payload["http_headers"])
    assert "观测简报" in payload["payload_text"]
    # 记录标识与目标 URL 分别展示
    assert payload["summary"]["record_id"].startswith("urn:uuid:")
    assert payload["summary"]["target_uri"].startswith("http://")


def test_raw_record_404_for_unknown_job(client):
    resp = client.get("/api/jobs/nope/records/0")
    assert resp.status_code == 404


def test_acceptance_endpoint(client):
    resp = client.post("/api/acceptance")
    job_id = resp.get_json()["job_id"]
    data = _wait_job(client, job_id)
    assert data["status"] == "done"
    assert data["result"]["record_type_counts"]["revisit"] == 3


def test_malformed_warc_returns_structured_error(tmp_path, monkeypatch):
    import warc_tool.web_app as web_app
    monkeypatch.setattr(web_app, "UPLOAD_DIR", tmp_path / "uploads2")
    monkeypatch.setattr(web_app, "SAMPLE_DIR", tmp_path / "samples2")
    app = create_app()
    client = app.test_client()
    bad = tmp_path / "bad.warc.gz"
    bad.write_bytes(b"this is not a warc file at all")
    with open(bad, "rb") as fh:
        job_id = client.post("/api/upload",
                             data={"warc": (fh, "bad.warc.gz")},
                             content_type="multipart/form-data").get_json()["job_id"]
    data = _wait_job(client, job_id)
    # 流水线对损坏输入不崩溃：done + 成员错误，或 error + 结构化消息
    assert data["status"] in ("done", "error")
    if data["status"] == "done":
        assert data["result"]["member_errors"] or data["result"]["total_records"] == 0
