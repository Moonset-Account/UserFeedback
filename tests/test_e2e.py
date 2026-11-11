"""端到端验证：真实 Playwright 执行样例任务，结果与独立真值逐项断言。

标记 slow：需要浏览器；正常+边界小样例另有 15 秒预算检查。
"""
from __future__ import annotations

import json
import time

import pytest

from pathlib import Path

from src.rpa.executor import run_task_file


def _truth(truth_dir, task_file):
    path = Path(truth_dir) / task_file.replace(".json", ".truth.json")
    return json.loads(path.read_text(encoding="utf-8"))["expectations"]


def _run(samples_dir, run_dir, server, task_file):
    return run_task_file(
        Path(samples_dir) / task_file, server, Path(run_dir) / Path(task_file).stem
    )


def test_demo_normal_same_name(server, samples_dir, truth_dir, run_dir):
    task_file = "task-normal-same-name.json"
    truth = _truth(truth_dir, task_file)
    report = _run(samples_dir, run_dir, server, task_file)

    assert report.status == truth["expect_status"] == "success"
    open_steps = [s for s in report.steps if s["action"] == "open_path"]
    assert len(open_steps) == 1
    assert open_steps[0]["observed"]["clicked_node_id"] == truth["must_click_node_id"]
    # 决策证据必须说明“为什么选它”。
    decision = open_steps[0]["decision"]
    assert decision["status"] == "unique"
    assert decision["node_id"] == "thermal.start"
    assert len(decision["candidates"]) == 1
    # 可见状态验证步骤真实通过。
    verify_steps = [s for s in report.steps if s["action"] == "expect_state"]
    assert verify_steps and "通过" in verify_steps[0]["summary"]
    # 截图证据存在。
    assert list((Path(run_dir) / Path(task_file).stem / "evidence").glob("*.png"))


def test_demo_boundary_disabled_stops(server, samples_dir, truth_dir, run_dir):
    task_file = "task-boundary-disabled.json"
    truth = _truth(truth_dir, task_file)
    report = _run(samples_dir, run_dir, server, task_file)

    assert report.status == "failed"
    assert report.error["code"] == truth["expect_error_code"] == "COMMAND_DISABLED"
    # 没有任何点击步骤产生 DOM 点击。
    assert not any(s["action"] == "open_path" and s.get("observed") for s in report.steps)
    blocked = next(s for s in report.steps if s.get("decision"))
    assert blocked["decision"]["status"] == "disabled"
    assert blocked["decision"]["node_id"] == truth["blocked_node_id"]
    assert "停止" in blocked["summary"]
    # 设备状态未被改变（后续 expect_state 根本没机会执行）。
    assert len(report.steps) == 3  # switch_context, snapshot, open_path(stop)


def test_full_ambiguous_stops_without_click(server, samples_dir, truth_dir, run_dir):
    task_file = "task-full-ambiguous.json"
    truth = _truth(truth_dir, task_file)
    report = _run(samples_dir, run_dir, server, task_file)

    assert report.status == "failed"
    assert report.error["code"] == "AMBIGUOUS_COMMAND"
    step = report.steps[-1]
    assert step["decision"] is not None
    candidate_ids = {c["id"] for c in step["decision"]["candidates"]}
    assert candidate_ids == set(truth["ambiguous_candidate_ids"])
    assert step["observed"] is None  # 未执行点击


def test_full_context_switch_changes_availability(server, samples_dir, truth_dir, run_dir):
    task_file = "task-full-context-switch.json"
    truth = _truth(truth_dir, task_file)
    report = _run(samples_dir, run_dir, server, task_file)

    assert report.status == "success"
    clicked = [
        s["observed"]["clicked_node_id"]
        for s in report.steps
        if s["action"] == "open_path"
    ]
    assert clicked == truth["must_click_node_ids"]
    contexts = [s["observed"]["visible"] for s in report.steps if s["action"] == "switch_context"]
    assert contexts == ["ALPHA 标准实验", "BETA 极限实验"]


def test_full_search_reorders_across_contexts(server, samples_dir, truth_dir, run_dir):
    task_file = "task-full-search-reorder.json"
    truth = _truth(truth_dir, task_file)
    report = _run(samples_dir, run_dir, server, task_file)

    assert report.status == "success"
    searches = [s["observed"] for s in report.steps if s["action"] == "search_commands"]
    assert len(searches) == truth["search_step_count"]
    alpha_ids = [r["id"] for r in searches[0]["ordered_results"]]
    beta_ids = [r["id"] for r in searches[1]["ordered_results"]]
    assert alpha_ids[0] == truth["alpha_top_id"]
    assert beta_ids[0] == truth["beta_top_id"]
    assert alpha_ids != beta_ids  # 顺序确实重排
    # 两次搜索都包含同名“启动”命令，证明重排而非缺项。
    assert {"thermal.start", "pump.start", "light.on"} <= set(alpha_ids)
    assert {"thermal.start", "pump.start", "light.on"} <= set(beta_ids)
    # 禁用项仍出现在 BETA 结果中且标注 enabled=false。
    beta_pump = next(r for r in searches[1]["ordered_results"] if r["id"] == "pump.start")
    assert beta_pump["enabled"] is False


def test_full_dynamic_preset_submenu(server, samples_dir, truth_dir, run_dir):
    task_file = "task-full-dynamic-preset.json"
    truth = _truth(truth_dir, task_file)
    report = _run(samples_dir, run_dir, server, task_file)

    assert report.status == "success"
    step = next(s for s in report.steps if s["action"] == "open_path")
    assert step["observed"]["clicked_node_id"] == truth["must_click_node_id"]
    verify = next(s for s in report.steps if s["action"] == "expect_state")
    assert verify["observed"]["text"] == "BETA-压力"


def test_full_shortcut_conflict_visible_but_path_still_works(
    server, samples_dir, truth_dir, run_dir
):
    task_file = "task-full-shortcut-conflict.json"
    truth = _truth(truth_dir, task_file)
    report = _run(samples_dir, run_dir, server, task_file)

    assert report.status == "success"
    snap_step = next(s for s in report.steps if s["action"] == "snapshot_menu")
    # 快照证据中可直接看到冲突清单。
    # （冲突清单也由页面横幅展示，截图 step 已保存。）
    open_step = next(s for s in report.steps if s["action"] == "open_path")
    assert open_step["observed"]["clicked_node_id"] == truth["must_click_node_id"]
    assert snap_step["screenshot"]


def test_demo_pair_finishes_within_15_seconds(server, samples_dir, run_dir):
    """验收硬性预算：正常+边界两个小样例串行执行 ≤ 15 秒。"""
    started = time.time()
    r1 = run_task_file(
        Path(samples_dir) / "task-normal-same-name.json",
        server,
        Path(run_dir) / "demo-normal",
    )
    r2 = run_task_file(
        Path(samples_dir) / "task-boundary-disabled.json",
        server,
        Path(run_dir) / "demo-boundary",
    )
    elapsed = time.time() - started
    assert r1.status == "success"
    assert r2.status == "failed" and r2.error["code"] == "COMMAND_DISABLED"
    assert elapsed <= 15, f"演示耗时 {elapsed:.1f}s 超过 15 秒预算"


def test_no_index_based_click_in_collector_source():
    """源码守卫：执行器不得出现 nth(序号) 这类固定序号点击。"""
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src" / "rpa" / "collector.py"
    ).read_text(encoding="utf-8")
    assert ".nth(" not in source
    assert "data-id" in source and "data-path" in source


@pytest.mark.parametrize(
    "task_file",
    [
        "task-normal-same-name.json",
        "task-boundary-disabled.json",
        "task-full-context-switch.json",
        "task-full-search-reorder.json",
        "task-full-ambiguous.json",
        "task-full-dynamic-preset.json",
        "task-full-shortcut-conflict.json",
    ],
)
def test_all_scenarios_match_ground_truth(
    server, samples_dir, truth_dir, run_dir, task_file
):
    """汇总断言：每个样例的最终状态必须与独立真值一致。"""
    truth = _truth(truth_dir, task_file)
    report = _run(samples_dir, run_dir, server, task_file)
    assert report.status == truth["expect_status"]
    if "expect_error_code" in truth:
        assert report.error["code"] == truth["expect_error_code"]
