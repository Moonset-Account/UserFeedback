"""服务端集成测试：源站公开接口 + 采集 API + 真实 HTTP 传输。

采集端通过 httpx 访问源站 NDJSON 流，与浏览器/外部采集者使用完全相同的公开接口；
测试断言只来自 expected_truth，不读取引擎内部对象。
"""
from __future__ import annotations

import json

import httpx
import pytest

from expected_truth import DEMO_EDGE_EXPECTED, DEMO_NORMAL_EXPECTED
from web.app import create_app


@pytest.fixture()
def app(tmp_path):
    application = create_app(recordings_dir=tmp_path / "recordings")
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def base_url(app):
    with app.test_client() as client:
        # test_client 不占真实端口；HTTP 传输测试改用 live server 夹具
        yield client


@pytest.fixture()
def rip_server(app):
    from werkzeug.serving import make_server
    import threading

    server = make_server("127.0.0.1", 0, app, threaded=True)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    thread.join(timeout=5)


def test_source_lists_scenarios(base_url):
    resp = base_url.get("/source/scenarios")
    assert resp.status_code == 200
    keys = {s["key"] for s in resp.get_json()["scenarios"]}
    assert {"demo_normal", "demo_edge", "comprehensive"} <= keys


def test_source_stream_is_ndjson_with_protocol_header(rip_server):
    with httpx.stream("GET", f"{rip_server}/source/stream/demo_normal", timeout=10) as r:
        assert r.status_code == 200
        assert r.headers["X-Protocol"] == "RIP/1.0"
        assert "ndjson" in r.headers["content-type"]
        first = json.loads(next(r.iter_lines()))
        assert first["type"] == "initial"
        assert first["version"] == "RIP/1.0"


def test_unknown_scenario_returns_404(rip_server):
    resp = httpx.get(f"{rip_server}/source/stream/nope", timeout=5)
    assert resp.status_code == 404


def test_http_collection_normal_end_to_end(rip_server):
    """完整链路：采集器只凭公开 HTTP 接口取数，结果对照独立真值。"""
    from rip_collector import CollectorEngine
    from rip_collector.transport import run_http_collection

    engine = run_http_collection(
        CollectorEngine(), f"{rip_server}/source/stream/demo_normal"
    )
    result = engine.result()
    assert result["status"] == "complete"
    assert result["final_snapshot"]["tree"] == DEMO_NORMAL_EXPECTED["tree"]


def test_http_collection_interrupted_without_end(rip_server):
    """边界链路：源站提前关闭连接且无 end，采集器必须标记 interrupted。"""
    from rip_collector import CollectorEngine
    from rip_collector.transport import run_http_collection

    engine = run_http_collection(
        CollectorEngine(), f"{rip_server}/source/stream/demo_edge"
    )
    result = engine.result()
    assert result["status"] == DEMO_EDGE_EXPECTED["status"]
    assert result["complete"] is False
    assert result["interrupt_reason"] == "stream_closed_without_end"
    assert all(s["complete"] is False for s in engine.snapshots)


def test_collector_api_full_task_lifecycle(app, rip_server, monkeypatch):
    """通过 /api 接口创建任务、轮询到结束、读取证据。"""
    # 让任务管理器拼出的源站地址指向本测试的随机端口
    app.config["TASK_MANAGER"]._source_base_url = rip_server
    client = app.test_client()

    resp = client.post("/api/tasks", json={
        "scenario_key": "demo_normal", "transport": "http"
    })
    assert resp.status_code == 202
    task_id = resp.get_json()["task_id"]

    # 轮询（任务很小，10 秒预算足够）
    import time
    deadline = time.monotonic() + 10
    view = None
    while time.monotonic() < deadline:
        view = client.get(f"/api/tasks/{task_id}").get_json()
        if view["status"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    assert view is not None and view["status"] == "complete"

    result = view["result"]
    assert result["final_snapshot"]["tree"]["id"] == "P-1001"
    assert result["decisions"], "必须保留逐条来源证据"
    assert any(not d["accepted"] for d in result["decisions"]), "重复补丁应留拒绝证据"


def test_file_transport_task(app):
    client = app.test_client()
    resp = client.post("/api/tasks", json={
        "scenario_key": "demo_normal", "transport": "file"
    })
    assert resp.status_code == 202
    task_id = resp.get_json()["task_id"]

    import time
    deadline = time.monotonic() + 10
    view = None
    while time.monotonic() < deadline:
        view = client.get(f"/api/tasks/{task_id}").get_json()
        if view["status"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    assert view["status"] == "complete"
    assert view["transport"] == "file"
    assert view["target"].endswith("demo_normal.ndjson")


def test_demo_endpoint_runs_both_scenarios(app, monkeypatch):
    app.config["TASK_MANAGER"]._source_base_url = "http://127.0.0.1:9"  # 文件传输不联网
    client = app.test_client()
    resp = client.post("/api/demo", json={"transport": "file"})
    assert resp.status_code == 202
    ids = resp.get_json()["task_ids"]
    assert len(ids) == 2

    import time
    views = []
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        views = [client.get(f"/api/tasks/{i}").get_json() for i in ids]
        if all(v["status"] not in ("queued", "running") for v in views):
            break
        time.sleep(0.1)
    statuses = {v["scenario_key"]: v["status"] for v in views}
    assert statuses == {"demo_normal": "complete", "demo_edge": "interrupted"}


def test_index_page_serves_chinese_ui(base_url):
    html = base_url.get("/").get_data(as_text=True)
    assert "科研资料增量接口采集" in html
    assert 'id="run-demo"' in html
    assert "运行示例" in html
