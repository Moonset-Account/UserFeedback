"""Web 接口冒烟测试：公开接口、协议拒绝、采集边界白名单。"""
from __future__ import annotations

import json
import urllib.request


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post(base, path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_health_and_snapshot_public(server):
    status, body = _get(server, "/lab/api/health")
    assert status == 200 and body["real_device"] is False
    status, body = _get(server, "/lab/api/snapshot")
    assert status == 200 and body["protocol"] == "lab.snapshot.v1"
    assert body["context"] in ("ALPHA", "BETA")


def test_snapshot_contains_three_same_name_commands(server):
    _, body = _get(server, "/lab/api/snapshot")
    labels = []

    def walk(nodes):
        for n in nodes:
            if n["kind"] == "command":
                labels.append((n["label"], n["id"], n["enabled"]))
            walk(n.get("children", []))

    walk(body["menu"])
    starts = [x for x in labels if x[0] == "启动"]
    assert {x[1] for x in starts} == {"thermal.start", "pump.start", "light.on"}


def test_disable_rules_context_sensitive(server):
    _, alpha = _get(server, "/lab/api/snapshot")
    _post(server, "/lab/api/context", {"context": "BETA"})
    _, beta = _get(server, "/lab/api/snapshot")
    pump_alpha = next(
        n for root in alpha["menu"] for n in _all(root) if n.get("id") == "pump.start"
    )
    pump_beta = next(
        n for root in beta["menu"] for n in _all(root) if n.get("id") == "pump.start"
    )
    assert pump_alpha["enabled"] is True
    assert pump_beta["enabled"] is False
    assert "联锁" in pump_beta["disabled_reason"]
    _post(server, "/lab/api/context", {"context": "ALPHA"})


def _all(node):
    out = [node]
    for child in node.get("children", []):
        out.extend(_all(child))
    return out


def test_run_rejects_protocol_violation(server):
    status, body = _post(
        server,
        "/api/run",
        {"task_document": {"protocol": "wrong", "name": "x", "steps": []}},
    )
    assert status == 422
    assert body["error"]["code"] == "PROTOCOL_VIOLATION"


def test_scenarios_listed_without_truth_exposure(server):
    _, body = _get(server, "/api/scenarios")
    files = {s["file"] for s in body["scenarios"]}
    assert "task-normal-same-name.json" in files
    assert all("truth" not in f for f in files)


def test_dynamic_presets_differ_by_context(server):
    _, alpha = _get(server, "/lab/api/snapshot")
    _post(server, "/lab/api/context", {"context": "BETA"})
    _, beta = _get(server, "/lab/api/snapshot")
    a = {p["id"] for p in alpha["presets"]}
    b = {p["id"] for p in beta["presets"]}
    assert a and b and a.isdisjoint(b)
    _post(server, "/lab/api/context", {"context": "ALPHA"})


def test_server_rejects_disabled_command(server):
    _post(server, "/lab/api/context", {"context": "BETA"})
    status, body = _post(server, "/lab/api/command", {"command_id": "pump.start"})
    assert status == 409 and body["ok"] is False
    _post(server, "/lab/api/context", {"context": "ALPHA"})
