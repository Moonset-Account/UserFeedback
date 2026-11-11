"""样例与公开协议契约测试（含独立预期断言）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from curve_rpa.sample_gen import TASKS_DIR, TRUTH_DIR, build_samples, build_truth, write_samples
from curve_rpa.task_schema import SCHEMA_VERSION, editor_view, parse_task

ROOT = Path(__file__).resolve().parents[1]


def test_generated_samples_match_fixture():
    before = {p.name: p.read_text(encoding="utf-8") for p in TASKS_DIR.glob("*.json")}
    write_samples()
    after = {p.name: p.read_text(encoding="utf-8") for p in TASKS_DIR.glob("*.json")}
    assert set(before) == set(after)
    for name, content in before.items():
        assert after[name] == content, f"样例生成非幂等：{name}"


def test_all_samples_parse_under_v1_protocol():
    for raw in build_samples():
        task = parse_task(raw)
        assert raw["schema_version"] == SCHEMA_VERSION
        assert len(task.initial_points) == len(task.target_points)


def test_invalid_protocol_rejected():
    raw = build_samples()[0].copy()
    raw["schema_version"] = "curve-task/v999"
    with pytest.raises(ValueError):
        parse_task(raw)
    bad = build_samples()[0].copy()
    bad["domain"] = dict(bad["domain"], x_grid=-1)
    with pytest.raises(ValueError):
        parse_task(bad)


def test_editor_view_never_exposes_targets():
    task = parse_task(build_samples()[0])
    view = editor_view(task)
    assert "target_points" not in view
    assert "tolerance_x" not in view
    assert view["task_id"] == task.task_id
    assert view["initial_points"] == [[x, y] for x, y in task.initial_points]


def test_demo_normal_independent_expectations():
    """独立预期（硬编码断言，不依赖真值文件）：正常场景应可收敛。"""
    from curve_rpa.geometry import half_up_round

    task = parse_task(json.loads((TASKS_DIR / "demo-normal.json").read_text(encoding="utf-8")))
    assert task.domain.zoom != 1.0  # 非默认缩放
    d = task.domain
    for (tx, ty) in task.target_points:
        # 目标确实落在网格中间
        assert half_up_round(tx / d.x_grid) * d.x_grid != tx
        assert half_up_round(ty / d.y_grid) * d.y_grid != ty
        # 吸附误差 2.5，在 3.0 容限内
        assert abs(half_up_round(tx / d.x_grid) * d.x_grid - tx) == pytest.approx(2.5)
        assert task.tolerance_x >= 2.5


def test_demo_boundary_independent_expectations():
    task = parse_task(json.loads((TASKS_DIR / "demo-boundary.json").read_text(encoding="utf-8")))
    assert task.domain.inverted_y is True
    oob = [ty for _, ty in task.target_points if ty > task.domain.y_max]
    assert oob  # 至少一个越界目标
    truth = build_truth(task)
    assert truth["expected_status"] == "FAIL_OUT_OF_BOUNDS"


def test_order_violation_sample():
    task = parse_task(json.loads((TASKS_DIR / "accept-order.json").read_text(encoding="utf-8")))
    xs = [t[0] for t in task.target_points]
    assert xs[0] > xs[1]  # 目标顺序反转
    assert build_truth(task)["expected_status"] == "FAIL_ORDER"


def test_strict_tolerance_sample_is_mathematically_unreachable():
    task = parse_task(json.loads((TASKS_DIR / "accept-strict-tol.json").read_text(encoding="utf-8")))
    truth = build_truth(task)
    assert truth["expected_status"] == "FAIL_TOLERANCE_UNREACHABLE"
    for p in truth["points"]:
        assert p["snap_reachable_within_tolerance"] is False


def test_truth_files_present_and_consistent():
    for task_file in TASKS_DIR.glob("*.json"):
        if task_file.name == "index.json":
            continue
        task = parse_task(json.loads(task_file.read_text(encoding="utf-8")))
        truth_file = TRUTH_DIR / f"{task.task_id}.truth.json"
        assert truth_file.exists()
        truth = json.loads(truth_file.read_text(encoding="utf-8"))
        assert truth["task_id"] == task.task_id
        assert truth["expected_status"] in {
            "SUCCESS",
            "FAIL_ORDER",
            "FAIL_OUT_OF_BOUNDS",
            "FAIL_TOLERANCE_UNREACHABLE",
        }
        # 真值点数与任务一致，且所有吸附候选都落在网格线上
        assert len(truth["points"]) == len(task.target_points)
        for p in truth["points"]:
            for axis_idx, grid in ((0, task.domain.x_grid), (1, task.domain.y_grid)):
                for candidate in p["nearest_snap_options"][axis_idx]:
                    assert candidate % grid == pytest.approx(0.0, abs=1e-9)
            ax, ay = p["expected_actual"]
            assert ax in p["nearest_snap_options"][0]
            assert ay in p["nearest_snap_options"][1]
