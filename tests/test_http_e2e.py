"""HTTP 全链路端到端测试：真实 Werkzeug 服务 + 真实浏览器访问公开 URL。

覆盖：根页面、场景接口、/api/demo 示例按钮后端（15 秒目标）、运行/详情/批准/重采 API，
以及采集器与真值目录的代码级隔离检查。
"""

import json
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_root_page_serves_chinese_ui(flask_server):
    resp = flask_server["client"].get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "科研测量页面语义契约采集器" in html
    assert 'id="run-demo"' in html
    assert "运行示例" in html


def test_scenarios_endpoint(flask_server):
    data = flask_server["client"].get("/api/scenarios").get_json()
    keys = {s["key"] for s in data}
    assert {"baseline", "restyle", "label", "unit", "no-qual", "median", "type-error"} <= keys


def test_sample_page_publicly_served(flask_server):
    resp = flask_server["client"].get("/samples/label/index.html")
    assert resp.status_code == 200
    assert "样品湿重" in resp.get_data(as_text=True)


def test_full_http_flow_run_inspect_approve_rerun(flask_server):
    base = flask_server["base"]
    client = flask_server["client"]

    # 1) 通过公开 HTTP URL 跑标签变更场景（浏览器真实访问）
    resp = client.post("/api/runs", json={
        "scenario": "label",
        "url": f"{base}/samples/label/index.html",
    })
    assert resp.status_code == 200
    summary = resp.get_json()
    run_id = summary["run_id"]
    assert summary["status"] == "quarantined"
    assert "LABEL_MISMATCH" in summary["violation_codes"]
    assert summary["published_count"] == 0
    assert summary["quarantined_count"] == 4

    # 2) 详情与证据接口
    detail = client.get(f"/api/runs/{run_id}").get_json()
    assert detail["evidence"]["access"] == "browser"
    shot = client.get(f"/api/runs/{run_id}/screenshot.png")
    assert shot.status_code == 200 and shot.content_type == "image/png"
    table = client.get(f"/api/runs/{run_id}/table.html")
    assert 'id="measure"' in table.get_data(as_text=True)

    # 3) 未批准前不存在 v2，正式结果为空
    contracts_before = client.get("/api/contracts").get_json()
    assert [c["version"] for c in contracts_before] == ["v1"]

    # 4) 批准
    suggestions = client.get(f"/api/runs/{run_id}/suggestions").get_json()
    approval = client.post(f"/api/runs/{run_id}/approve", json={
        "field_keys": ["dry_weight"],
        "approved_by": "审核员-陈",
        "reason": "HTTP 全链路批准测试",
        "overrides": {"dry_weight": suggestions["dry_weight"]},
    }).get_json()
    assert approval["new_contract_version"] == "v2"
    assert approval["affected_scope"]["record_count"] == 4

    approvals = client.get("/api/approvals").get_json()
    assert approvals[0]["old_rules"]["dry_weight"]["key"] == "dry_weight"

    # 5) 用 v2 重采，全部发布
    rerun = client.post("/api/runs", json={
        "scenario": "label",
        "url": f"{base}/samples/label/index.html",
        "contract_version": "v2",
    }).get_json()
    assert rerun["status"] == "passed"
    assert rerun["published_count"] == 4


def test_demo_endpoint_finishes_within_15s(flask_server):
    t0 = time.time()
    resp = flask_server["client"].post("/api/demo")
    elapsed = time.time() - t0
    assert resp.status_code == 200
    runs = resp.get_json()["demo_runs"]
    assert [r["scenario"] for r in runs] == ["demo-baseline", "demo-label"]
    assert runs[0]["status"] == "passed"
    assert runs[1]["status"] == "quarantined"
    # 两个结果都保留可查
    for r in runs:
        detail = flask_server["client"].get(f"/api/runs/{r['run_id']}")
        assert detail.status_code == 200
    assert elapsed < 15, f"示例运行耗时 {elapsed:.1f}s 超过 15 秒目标"


def test_unknown_url_returns_error_not_fake_success(flask_server):
    resp = flask_server["client"].post("/api/runs", json={
        "scenario": "custom",
        "url": "http://127.0.0.1:1/never-exists",
    })
    assert resp.status_code == 502
    assert "采集失败" in resp.get_json()["error"]


def test_approve_requires_field_keys(flask_server):
    client = flask_server["client"]
    run_id = client.post("/api/runs", json={"scenario": "label"}).get_json()["run_id"]
    resp = client.post(f"/api/runs/{run_id}/approve", json={"field_keys": []})
    assert resp.status_code == 400


def test_collector_code_never_reads_truth_directory():
    """代码级隔离：采集器包内不得出现对 truth 目录/真值文件的任何引用。"""
    for py in (ROOT / "collector").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "truth" not in text.lower(), f"{py.name} 引用了真值目录，违反隔离要求"
    # app.py 同样只接触 generated/samples，不接触 truth
    app_text = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "truth" not in app_text.lower()
