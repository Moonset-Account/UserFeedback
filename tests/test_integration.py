"""集成测试：通过 Flask 测试客户端走完整 WSGI 栈（真实路由/解析/过滤）。"""

from __future__ import annotations

import json

import pytest

from collector import CollectorClient
from server.app import create_app


@pytest.fixture(scope="module")
def app():
    app = create_app({"TESTING": True, "BASE_URL": "http://127.0.0.1:0"})
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["rows"] == 24


def test_origin_echoes_raw_request_and_semantics(client):
    # 直接请求源站公开接口，检查回显的原始串与语义解析
    resp = client.get(
        "/v1/experiments?tags=%E8%8A%AF%E7%89%87%2C%E5%BE%AE%E6%B5%81"
        "&tags=%E6%89%B9%E6%AC%A1%2F%E4%BA%8C&levels=B"
    )
    assert resp.status_code == 200
    body = resp.get_json()
    echo = body["echo"]
    assert echo["raw_query_string"].startswith("tags=")
    assert echo["query_parameters"] == {
        "tags": ["芯片,微流", "批次/二"],
        "levels": ["B"],
    }
    assert [r["id"] for r in body["data"]] == ["EXP-013"]


def test_origin_deep_object_and_allow_reserved(client):
    # 先验证 allowReserved 的原始回显（关键字不命中数据不影响回显校验）
    resp = client.get("/v1/experiments?q=a/b,c:d%3De%23f")
    body = resp.get_json()
    echo = body["echo"]
    assert "q=a/b,c:d%3De%23f" in echo["raw_query_string"]
    assert echo["query_parameters"]["q"] == "a/b,c:d=e#f"
    assert body["data"] == []

    # 再验证 deepObject 的语义筛选
    resp2 = client.get(
        "/v1/experiments?filter%5Bstatus%5D=running&filter%5Bmin_score%5D=90"
    )
    body2 = resp2.get_json()
    assert body2["echo"]["query_parameters"]["filter"] == {
        "status": "running", "min_score": 90,
    }
    assert {r["id"] for r in body2["data"]} == {
        "EXP-001", "EXP-009", "EXP-012", "EXP-020"
    }


def test_origin_path_with_encoded_slash_routes(client):
    resp = client.get("/v1/experiments/EXP%2F002%2Cnext")
    assert resp.status_code == 404
    echo = resp.get_json()["echo"]
    assert echo["path_parameters"] == {"exp_id": "EXP/002,next"}
    assert echo["raw_path"] == "/v1/experiments/EXP%2F002%2Cnext"


def test_origin_path_hit_and_lab_route(client):
    resp = client.get("/v1/experiments/EXP-002")
    assert resp.status_code == 200
    assert resp.get_json()["data"]["id"] == "EXP-002"

    resp2 = client.get(
        "/v1/labs/.%E5%85%89%E5%AD%A6%2C%E7%BB%84,"
        "%E4%BD%8E%E6%B8%A9%2F%E7%BB%84/experiments/;id=EXP-001;rev=3"
    )
    assert resp2.status_code == 200
    body2 = resp2.get_json()
    assert body2["data"]["id"] == "EXP-001"
    assert body2["echo"]["path_parameters"] == {
        "lab": ["光学,组", "低温/组"],
        "exp_id": {"id": "EXP-001", "rev": 3},
    }


def test_origin_rejects_non_get(client):
    resp = client.post("/v1/experiments")
    assert resp.status_code == 405


def test_web_preview_does_not_send_and_matches_golden(spec, client):
    resp = client.post("/api/preview", json={
        "operationId": "listExperiments",
        "params": {"codes": ["TB-21,7", "MB/330"]},
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["request"]["query_string"] == "codes=TB-21%2C7,MB%2F330"
    # 预览不产生响应体
    assert "response" not in body


def test_web_run_rejection_happens_before_request(client):
    resp = client.post("/api/run", json={
        "operationId": "listExperiments",
        "params": {"filter": ["running", "paused"]},
    })
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["rejected_before_request"] is True
    assert "期望 object" in body["error"]
    assert "request" not in body


def test_web_run_unknown_param_rejected(client):
    resp = client.post("/api/run", json={
        "operationId": "listExperiments",
        "params": {"inject": "1=1"},
    })
    assert resp.status_code == 422
    assert "文档未声明" in resp.get_json()["error"]


def test_collector_client_real_loopback(app):
    """用 requests 对真实监听端口发请求（live server）。"""
    from werkzeug.serving import make_server
    import threading

    server = make_server("127.0.0.1", 0, app)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        collector = CollectorClient(app.config["SPEC"],
                                    f"http://127.0.0.1:{port}")
        result = collector.run("listExperiments", {
            "tags": ["芯片,微流", "批次/二"],
            "codes": [],
            "levels": ["B"],
        })
        assert result.status_code == 200
        assert result.error is None
        assert [r["id"] for r in result.data] == ["EXP-013"]
        # 回显中的原始 query 必须保留 %2C / %2F 编码
        raw = result.echo["raw_query_string"]
        assert "%2C" in raw and "%2F" in raw
        assert result.request.omitted == ["codes"]

        # 拒绝场景：构造失败时不产生网络请求
        with pytest.raises(Exception):
            collector.preview("listExperiments", {"filter": [1, 2]})
    finally:
        server.shutdown()
