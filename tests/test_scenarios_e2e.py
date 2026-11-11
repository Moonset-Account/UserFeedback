"""场景级端到端：对真实监听端口执行 fixtures/scenarios.json 全部场景，
并与 generator 产出的独立真值（data/ground_truth.json）逐条断言。

注意：本测试通过公开 HTTP 接口采集数据，ground_truth 仅用于断言，
采集器代码路径不读取该文件。
"""

from __future__ import annotations

import json
import threading

import pytest
import requests
from werkzeug.serving import make_server

from collector import CollectorClient, UnsupportedCombinationError, ValidationError
from server.app import create_app


@pytest.fixture(scope="module")
def live_server():
    app = create_app({"BASE_URL": "http://127.0.0.1:0"})
    server = make_server("127.0.0.1", 0, app)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", app
    server.shutdown()


def _all_scenarios(scenarios):
    for group in ("run_demo", "acceptance"):
        for sc in scenarios.get(group, []):
            if not sc.get("disabled"):
                yield sc


def test_every_scenario_against_ground_truth(live_server, scenarios, ground_truth):
    base_url, app = live_server
    collector = CollectorClient(app.config["SPEC"], base_url)
    gt = ground_truth["scenarios"]

    for sc in _all_scenarios(scenarios):
        truth = gt[sc["id"]]
        if sc["kind"] == "rejection":
            # 必须在发请求前拒绝
            with pytest.raises(
                (UnsupportedCombinationError, ValidationError),
                match=sc["expect"]["error_contains"],
            ):
                collector.preview(sc["operationId"], sc.get("params") or {})
            continue

        # 1) 构造出的请求与真值中的精确期望一致
        built = collector.preview(sc["operationId"], sc.get("params") or {})
        assert built.method == "GET"
        assert built.query_string == truth["request"]["query_string"], sc["id"]
        assert built.path == truth["request"]["path"], sc["id"]
        assert built.omitted == truth["request"]["omitted"], sc["id"]

        # 2) 真实发送并核对 HTTP 状态与返回条目
        result = collector.run(sc["operationId"], sc.get("params") or {})
        assert result.error is None, sc["id"]
        assert result.status_code == sc["expect"]["status_code"], sc["id"]

        if isinstance(result.data, list):
            got_ids = [r["id"] for r in result.data]
        elif result.data is None:
            got_ids = []
        else:
            got_ids = [result.data["id"]]

        if "result_ids_ordered" in sc["expect"]:
            assert got_ids == sc["expect"]["result_ids_ordered"], sc["id"]
        elif "result_ids" in sc["expect"]:
            assert set(got_ids) == set(sc["expect"]["result_ids"]), sc["id"]

        # 3) 与独立真值交叉一致
        assert set(got_ids) == set(truth.get("result_ids", [])), sc["id"]

        # 4) 回显必须包含原始报文与语义解析两部分
        assert result.echo["raw_query_string"] == built.query_string
        assert "query_parameters" in result.echo or "path_parameters" in result.echo


def test_collector_never_reads_ground_truth_file(project_root):
    """架构约束：collector 包内不得出现对 ground_truth 的读取。"""
    for py in (project_root / "collector").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "ground_truth" not in text, f"{py} 不得引用独立真值文件"


def test_raw_http_matches_golden_string(live_server):
    """绕过采集器，用裸 requests 发黄金串，证明源站按原始报文解析。"""
    base_url, _ = live_server
    golden = (
        "/v1/experiments?codes=TB-21%2C7,MB%2F330"
        "&scores=92%2095"
    )
    resp = requests.get(base_url + golden, timeout=10)
    assert resp.status_code == 200
    echo = resp.json()["echo"]
    assert echo["raw_query_string"] == golden.split("?", 1)[1]
    assert echo["query_parameters"]["codes"] == ["TB-21,7", "MB/330"]
    assert echo["query_parameters"]["scores"] == [92, 95]
