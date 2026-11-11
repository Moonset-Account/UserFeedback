"""端到端验证：通过真实 Playwright 浏览器执行 RPA，并与独立真值比对。

覆盖验收要求：
* 非默认缩放/平移/反向纵轴（demo-normal、accept-inverted）
* 落在网格中间的目标（所有 SUCCESS 场景）
* 越界目标必须明确失败（demo-boundary -> FAIL_OUT_OF_BOUNDS）
* 两点不可交叉约束（accept-order -> FAIL_ORDER）
* 容限不可达必须明确失败（accept-strict-tol -> FAIL_TOLERANCE_UNREACHABLE）
* 失败不能凭像素判定：断言引擎结果基于页面显示数值误差
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from curve_rpa.collector import HttpTaskCollector
from curve_rpa.engine import CurveEditorAutomation
from curve_rpa.sample_gen import TRUTH_DIR

from .conftest import e2e

ALL_TASK_IDS = [
    "demo-normal",
    "demo-boundary",
    "accept-order",
    "accept-strict-tol",
    "accept-inverted",
]


def _truth(task_id: str) -> dict:
    return json.loads((TRUTH_DIR / f"{task_id}.truth.json").read_text(encoding="utf-8"))


@e2e
@pytest.mark.parametrize("task_id", ALL_TASK_IDS)
def test_full_e2e_matches_independent_truth(live_server, task_id):
    automation = CurveEditorAutomation(base_url=live_server, headless=True)
    result = automation.run(task_id)
    truth = _truth(task_id)

    # 1. 裁决状态必须与独立真值一致
    assert result.status == truth["expected_status"], (
        f"{task_id}: 引擎裁决 {result.status}，独立真值期望 {truth['expected_status']}，"
        f"原因：{result.failure_reason}"
    )

    # 2. 最终实际值必须是真值给出的最近网格吸附值之一（网格正中间存在两个等距吸附值），
    #    且误差幅度必须与离线真值一致——裁决基于页面真实显示数值而非像素
    for pr, tp in zip(result.point_results, truth["points"]):
        ax, ay = pr["final_actual"]
        assert ax in tp["nearest_snap_options"][0]
        assert ay in tp["nearest_snap_options"][1]
        assert abs(ax - pr["target"][0]) == pytest.approx(tp["expected_error"][0], abs=1e-9)
        assert abs(ay - pr["target"][1]) == pytest.approx(tp["expected_error"][1], abs=1e-9)
        # 误差字段与 目标-实际 一致，证明裁决基于数值误差
        assert pr["final_error"][0] == pytest.approx(pr["final_actual"][0] - pr["target"][0], abs=1e-12)
        assert pr["final_error"][1] == pytest.approx(pr["final_actual"][1] - pr["target"][1], abs=1e-12)

    # 3. 成功场景必须在容限内；失败场景必须给出明确失败原因
    if truth["expected_status"] == "SUCCESS":
        assert result.failure_reason is None
        assert all(p["within_tolerance"] for p in result.point_results)
    else:
        assert result.failure_reason
        assert result.status.startswith("FAIL_")

    # 4. 迭代证据完整且尝试次数受最大尝试次数约束
    task = HttpTaskCollector(live_server).fetch_task(task_id)
    for pr in result.point_results:
        assert 1 <= len(pr["attempts"]) <= task.max_attempts_per_point
        for a in pr["attempts"]:
            assert a["phase"] in {"drag", "keyboard-nudge", "keyboard-nudge(oscillation-break)"}

    # 5. 截图证据落盘
    shot = Path(__file__).resolve().parents[1] / result.evidence["screenshot"]
    assert shot.exists() and shot.stat().st_size > 10_000


@e2e
def test_mapping_recovers_inverted_and_zoomed_axes(live_server):
    """非默认缩放 + 反向纵轴下，由公开刻度反解的映射必须自洽并反映视图配置。"""
    result = CurveEditorAutomation(live_server, headless=True).run("accept-inverted")
    mx, my = result.mapping["x"], result.mapping["y"]
    # 按刻度数值排序后，斜率符号直接表达轴方向：x 与反向 y 均为正斜率
    assert mx["slope"] > 0
    assert my["slope"] > 0
    assert result.mapping["view_state"]["inverted_y"] is True
    assert result.mapping["view_state"]["zoom"] == pytest.approx(1.5)
    # zoom=1.5 相比基准（每单位约 7.92px）应让像素/数据比放大约 1.5 倍
    assert abs(mx["slope"]) > 10.0
    assert result.mapping["tick_count"]["x"] >= 3
    assert result.mapping["tick_count"]["y"] >= 3

    # 与普通纵轴（demo-normal, zoom=1.35）对比：普通 y 轴为负斜率，
    # 证明映射确实来自公开刻度而非写死方向
    normal = CurveEditorAutomation(live_server, headless=True).run("demo-normal")
    assert normal.mapping["view_state"]["inverted_y"] is False
    assert normal.mapping["y"]["slope"] < 0
    assert normal.mapping["y"]["slope"] != pytest.approx(my["slope"], rel=0.05)


@e2e
def test_demo_runs_within_15_seconds_budget(live_server):
    """两个快速演示场景顺序执行必须在 15 秒内结束（网页“运行示例”同款路径）。"""
    auto = CurveEditorAutomation(live_server, headless=True)
    start = time.time()
    r1 = auto.run("demo-normal")
    r2 = auto.run("demo-boundary")
    elapsed = time.time() - start
    assert elapsed < 15.0, f"演示耗时 {elapsed:.1f}s 超过 15 秒预算"
    assert r1.status == "SUCCESS"
    assert r2.status == "FAIL_OUT_OF_BOUNDS"


@e2e
def test_console_run_demo_button_and_jobs(live_server):
    """模拟网页控制台：运行示例按钮存在，且通过 /api/run 得到真实结果。"""
    import requests

    page_html = requests.get(f"{live_server}/", timeout=5).text
    assert 'id="run-demo"' in page_html
    assert "运行示例" in page_html

    for task_id in ["demo-normal", "demo-boundary"]:
        resp = requests.post(
            f"{live_server}/api/run", json={"task_id": task_id, "headless": True}, timeout=10
        )
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]
        final = _wait_job(live_server, job_id)
        assert final["status"] == "DONE"
        assert final["result"]["task_id"] == task_id
        assert "mapping" in final["result"] and "evidence" in final["result"]


def _wait_job(base_url: str, job_id: str, timeout: float = 30.0) -> dict:
    import requests

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = requests.get(f"{base_url}/api/jobs/{job_id}", timeout=5).json()
        if job["status"] in {"DONE", "ERROR"}:
            assert job["status"] == "DONE", job.get("error")
            return job
        time.sleep(0.5)
    raise AssertionError("任务执行超时")
