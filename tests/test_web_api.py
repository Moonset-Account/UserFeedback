"""Web API 测试：任务创建、轮询、结果与错误结构。"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from sampler.experiment_data import EXPERIMENT_ID, member_name
from source_site.server import start_source_server
from web.app import create_web_server


@pytest.fixture()
def web_stack(sample_data_dir):
    source_server, _st, source_base = start_source_server(str(sample_data_dir), "127.0.0.1", 0)
    web_server = create_web_server("127.0.0.1", 0, source_base, str(sample_data_dir))
    thread = threading.Thread(target=web_server.serve_forever, daemon=True)
    thread.start()
    host, port = web_server.server_address
    base = f"http://{host}:{port}"
    # 等待就绪
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/meta", timeout=1) as resp:
                if resp.status == 200:
                    break
        except OSError:
            time.sleep(0.1)
    yield base, source_base
    web_server.shutdown()
    source_server.shutdown()


def _post(base, path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_meta_and_index_page(web_stack):
    base, _source = web_stack
    status, meta = _get(base, "/api/meta")
    assert status == 200
    assert meta["experiment_id"] == EXPERIMENT_ID
    assert "ZIP64" in meta["protocol_subset"]
    assert len(meta["scenarios"]) >= 10
    with urllib.request.urlopen(base + "/", timeout=5) as resp:
        html = resp.read().decode("utf-8")
    assert "run-demo" in html
    assert "选择性采集" in html


def test_create_and_poll_success_task(web_stack):
    base, source = web_stack
    payload = {
        "url": source + "/packages/normal_small.zip",
        "wanted_members": [member_name(1, 1), member_name(3, 3)],
        "experiment_id": EXPERIMENT_ID,
        "label": "API-正常",
    }
    status, task = _post(base, "/api/tasks", payload)
    assert status == 201
    task_id = task["id"]
    # 轮询到结束
    deadline = time.time() + 15
    final = None
    while time.time() < deadline:
        _, final = _get(base, f"/api/tasks/{task_id}")
        if final["status"] == "finished":
            break
        time.sleep(0.2)
    assert final is not None and final["status"] == "finished"
    report = final["report"]
    assert report["status"] == "success"
    assert len(report["records"]) == 2
    assert report["traffic"]["downloaded_bytes"] > 0


def test_create_and_poll_failed_task(web_stack):
    base, source = web_stack
    payload = {
        "url": source + "/packages/crc_bad.zip",
        "wanted_members": [member_name(1, 1), member_name(3, 3)],
        "experiment_id": EXPERIMENT_ID,
        "label": "API-CRC错误",
    }
    _, task = _post(base, "/api/tasks", payload)
    deadline = time.time() + 15
    final = None
    while time.time() < deadline:
        _, final = _get(base, f"/api/tasks/{task['id']}")
        if final["status"] == "finished":
            break
        time.sleep(0.2)
    assert final["report"]["status"] == "failed"
    assert final["report"]["error"]["code"] == "crc_mismatch"
    assert final["report"]["records"] == []


def test_invalid_payload_rejected(web_stack):
    base, _source = web_stack
    req = urllib.request.Request(
        base + "/api/tasks",
        data=json.dumps({"url": ""}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("应当返回 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
