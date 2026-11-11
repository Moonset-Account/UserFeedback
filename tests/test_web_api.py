"""Web API 层测试：自定义输入接口与任务记录接口。"""
import json

import pytest


def test_custom_input_valid_runs_and_verifies(live_server):
    import urllib.request

    payload = {
        "mode": "check",
        "spec": {
            "protocol": "rpa-order/v1",
            "scenario_id": "api-custom-valid",
            "title": "接口自定义虚构实验",
            "steps": [
                {"id": "C1", "name": "准备", "group": "g"},
                {"id": "C2", "name": "处理", "group": "g"},
                {"id": "C3", "name": "观察"},
            ],
            "constraints": {"fixed": {"C1": 0}, "depends_on": {"C3": ["C2"]}},
        },
    }
    req = urllib.request.Request(
        f"{live_server}/api/run-custom",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        record = json.loads(resp.read().decode("utf-8"))
    assert record["status"] == "checked"
    assert record["dry_run_verification"]["ok"] is True
    # 同组约束（C1 -> C2）在独立验证中生效
    names = {c["name"]: c["ok"] for c in record["dry_run_verification"]["checks"]}
    assert all(names.values())


def test_custom_input_cycle_returns_conflict_chain(live_server):
    import urllib.request

    payload = {
        "mode": "check",
        "spec": {
            "protocol": "rpa-order/v1",
            "scenario_id": "api-custom-cycle",
            "title": "接口循环",
            "steps": [{"id": "a", "name": "甲"}, {"id": "b", "name": "乙"}],
            "constraints": {"depends_on": {"a": ["b"], "b": ["a"]}},
        },
    }
    req = urllib.request.Request(
        f"{live_server}/api/run-custom",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        record = json.loads(resp.read().decode("utf-8"))
    assert record["status"] == "unsatisfiable"
    assert any("循环依赖" in c for c in record["conflict_chains"])


def test_custom_input_malformed_protocol_rejected_400(live_server):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"{live_server}/api/run-custom",
        data=json.dumps({"spec": {"protocol": "wrong"}}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=15)
    assert ei.value.code == 400
    body = json.loads(ei.value.read().decode("utf-8"))
    assert "协议校验失败" in body["error"]


def test_runs_list_and_detail(live_server):
    import urllib.request

    with urllib.request.urlopen(f"{live_server}/api/runs", timeout=10) as resp:
        runs = json.loads(resp.read().decode("utf-8"))
    assert isinstance(runs, list) and runs
    run_id = runs[0]["run_id"]
    with urllib.request.urlopen(f"{live_server}/api/runs/{run_id}", timeout=10) as resp:
        detail = json.loads(resp.read().decode("utf-8"))
    assert detail["run_id"] == run_id


def test_unknown_run_returns_404(live_server):
    import urllib.error
    import urllib.request

    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(f"{live_server}/api/runs/nonexistent-id", timeout=10)
    assert ei.value.code == 404
