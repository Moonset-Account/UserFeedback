"""采集边界测试：采集器只能走公开 HTTP 接口，禁止读取本地真值/任务文件。"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import requests

from curve_rpa import collector as collector_mod
from curve_rpa import engine as engine_mod
from curve_rpa.collector import CollectorError, HttpTaskCollector

ROOT = Path(__file__).resolve().parents[1]
# 用拼接构造禁词，避免测试源码自身包含完整路径而自匹配
TRUTH_TOKEN = "data" + "/truth"
TASKS_TOKEN = "data" + "/tasks"
TRUTH_DIR = ROOT / TRUTH_TOKEN
TASKS_DIR = ROOT / TASKS_TOKEN


def test_collector_source_only_mentions_http():
    src = inspect.getsource(collector_mod)
    assert "requests.get" in src
    # 采集模块不得出现任何本地 data 目录路径或文件读取
    forbidden = [TRUTH_TOKEN, TASKS_TOKEN, "open(", "Path(", "read_text", "read_bytes"]
    for token in forbidden:
        assert token not in src, f"采集器出现禁止的本地访问: {token}"


def test_engine_never_references_truth_directory():
    for module in (engine_mod, collector_mod):
        src = inspect.getsource(module)
        assert TRUTH_TOKEN not in src
        assert "TRUTH_DIR" not in src


def test_collector_fetches_real_task_over_http(live_server):
    c = HttpTaskCollector(live_server)
    tasks = c.list_tasks()
    ids = {t["task_id"] for t in tasks}
    assert {"demo-normal", "demo-boundary", "accept-order", "accept-strict-tol", "accept-inverted"} <= ids

    task = c.fetch_task("demo-normal")
    assert task.task_id == "demo-normal"
    assert task.domain.zoom == pytest.approx(1.35)


def test_collector_rejects_bad_task_id(live_server):
    c = HttpTaskCollector(live_server)
    with pytest.raises(CollectorError):
        c.fetch_task("../etc-passwd")


def test_collector_404_surface(live_server):
    c = HttpTaskCollector(live_server)
    with pytest.raises(CollectorError):
        c.fetch_task("not-exist")


def test_editor_endpoint_does_not_leak_targets(live_server):
    resp = requests.get(f"{live_server}/api/editor/demo-normal", timeout=5)
    assert resp.status_code == 200
    payload = resp.json()
    assert "target_points" not in payload
    assert "tolerance_x" not in payload
    full = requests.get(f"{live_server}/api/tasks/demo-normal", timeout=5).json()
    assert "target_points" in full  # 完整规格接口才含目标


def test_truth_directory_has_no_http_route(live_server):
    # 源站不得暴露真值文件
    resp = requests.get(f"{live_server}/data/truth/demo-normal.truth.json", timeout=5)
    assert resp.status_code in (404, 403)
