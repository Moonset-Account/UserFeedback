"""样例生成器：生成本地源站使用的虚构校准任务与**独立真值**。

设计约束
========
* 全部数据为虚构，不依赖真实账户或外部业务服务；
* 采集器（engine.py / collector 层）只通过源站公开 HTTP 接口与浏览器 DOM 取数，
  本模块生成的 ``data/truth`` 真值文件**仅供测试断言**使用，采集器不得读取；
* 生成器可独立执行：``python -m curve_rpa.sample_gen`` 幂等重建样例。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .geometry import (
    clamp,
    expected_snap_error,
    half_up_round,
    is_mid_grid,
    out_of_bounds,
    snap_to_grid,
    violates_x_order,
)
from .task_schema import SCHEMA_VERSION, Task, parse_task

ROOT = Path(__file__).resolve().parents[2]
TASKS_DIR = ROOT / "data" / "tasks"
TRUTH_DIR = ROOT / "data" / "truth"


def _task(
    task_id: str,
    title: str,
    group: str,
    note: str,
    domain: dict[str, Any],
    initial: list[list[float]],
    targets: list[list[float]],
    max_attempts: int = 12,
    tolerance_x: float = 0.55,
    tolerance_y: float = 0.55,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "title": title,
        "group": group,
        "scenario_note": note,
        "domain": domain,
        "initial_points": initial,
        "target_points": targets,
        "max_attempts_per_point": max_attempts,
        "tolerance_x": tolerance_x,
        "tolerance_y": tolerance_y,
    }


def build_samples() -> list[dict[str, Any]]:
    """定义 5 个虚构场景：2 个快速演示 + 3 个完整验收。"""
    base_domain = {
        "x_min": 0.0,
        "x_max": 100.0,
        "y_min": 0.0,
        "y_max": 100.0,
        "x_grid": 5.0,
        "y_grid": 5.0,
        "inverted_y": False,
        "zoom": 1.0,
        "pan_x": 0.0,
        "pan_y": 0.0,
    }
    samples: list[dict[str, Any]] = []

    # 1) 快速演示-正常：非默认缩放 + 网格中间目标（靠网格吸附误差仍可在容限内收敛）
    #    目标均落在两条网格线之间（最近吸附误差 2.5），容限 3.0 可收敛
    d1 = dict(base_domain, zoom=1.35, pan_x=8.0, pan_y=-6.0)
    samples.append(
        _task(
            "demo-normal",
            "快速演示｜正常收敛（1.35 倍缩放，目标落在网格中间）",
            "quick",
            "非默认缩放与平移；目标不落在网格线上（最近网格误差 2.5），验证先建映射再迭代修正的闭环。",
            d1,
            initial=[[20.0, 30.0], [55.0, 20.0]],
            targets=[[27.5, 42.5], [62.5, 77.5]],
            max_attempts=12,
            tolerance_x=3.0,
            tolerance_y=3.0,
        )
    )

    # 2) 快速演示-边界：反向纵轴 + 越界目标（必须明确失败）
    d2 = dict(base_domain, inverted_y=True, zoom=0.9, pan_x=-4.0, pan_y=10.0)
    samples.append(
        _task(
            "demo-boundary",
            "快速演示｜越界失败（反向纵轴，目标超出轴域）",
            "quick",
            "第 2 个点的 y=118 超出 y_max=100，页面只能夹到边界，期望明确判失败。",
            d2,
            initial=[[15.0, 80.0], [70.0, 40.0]],
            targets=[[35.0, 55.0], [80.0, 118.0]],
            max_attempts=10,
            tolerance_x=3.0,
            tolerance_y=3.0,
        )
    )

    # 3) 完整验收-交叉约束：目标 x 顺序反转，页面阻止控制点交叉
    d3 = dict(base_domain, zoom=1.2, pan_x=5.0, pan_y=5.0)
    samples.append(
        _task(
            "accept-order",
            "完整验收｜两点不可交叉约束",
            "acceptance",
            "目标点 x=[60,40] 相对初始顺序 [25,70] 发生交叉，页面强制保持 x 严格递增。",
            d3,
            initial=[[25.0, 20.0], [70.0, 60.0]],
            targets=[[60.0, 35.0], [40.0, 80.0]],
            max_attempts=12,
            tolerance_x=3.0,
            tolerance_y=3.0,
        )
    )

    # 4) 完整验收-严格容限：网格中间目标 + 容差小于可达吸附误差，应失败
    d4 = dict(base_domain, zoom=0.8, pan_x=-10.0, pan_y=4.0)
    samples.append(
        _task(
            "accept-strict-tol",
            "完整验收｜容限不可达（吸附下界大于容差）",
            "acceptance",
            "目标 32.5 距最近网格线 2.5，而容差仅 0.1，任何拖动/微调都无法达标，必须判失败。",
            d4,
            initial=[[10.0, 10.0]],
            targets=[[32.5, 47.5]],
            max_attempts=10,
            tolerance_x=0.1,
            tolerance_y=0.1,
        )
    )

    # 5) 完整验收-反向纵轴综合：缩放 1.5 + 反向 y + 网格中间目标，正常收敛
    d5 = dict(base_domain, inverted_y=True, zoom=1.5, pan_x=12.0, pan_y=-8.0)
    samples.append(
        _task(
            "accept-inverted",
            "完整验收｜反向纵轴综合场景",
            "acceptance",
            "1.5 倍缩放、平移、反向纵轴同时存在，验证依据公开刻度反解映射的正确性。",
            d5,
            initial=[[20.0, 70.0], [50.0, 50.0], [80.0, 25.0]],
            targets=[[22.5, 62.5], [57.5, 37.5], [82.5, 12.5]],
            max_attempts=14,
            tolerance_x=3.0,
            tolerance_y=3.0,
        )
    )
    return samples


def build_truth(task: Task) -> dict[str, Any]:
    """根据公开领域规则推导独立真值（不执行浏览器，纯数学预期）。"""
    d = task.domain
    point_truth: list[dict[str, Any]] = []
    overall_structural_fail = False
    order_bad = violates_x_order([t[0] for t in task.target_points])
    if order_bad:
        overall_structural_fail = True

    # 离线模拟页面的顺序约束：x 严格递增且相邻点至少间隔一个网格步长。
    # 先各自吸附+轴域夹取，再从左到右施加最小间隔夹取，与编辑器 applyDataPosition 同规则。
    snapped: list[list[float]] = []
    for tx, ty in task.target_points:
        snapped.append(
            [
                clamp(snap_to_grid(tx, d.x_grid), d.x_min, d.x_max),
                clamp(snap_to_grid(ty, d.y_grid), d.y_min, d.y_max),
            ]
        )
    if order_bad:
        for i in range(1, len(snapped)):
            snapped[i][0] = clamp(
                max(snapped[i][0], snapped[i - 1][0] + d.x_grid), d.x_min, d.x_max
            )

    for i, ((tx, ty), (ix, iy)) in enumerate(zip(task.target_points, task.initial_points)):
        oob_x = out_of_bounds(tx, d.x_min, d.x_max)
        oob_y = out_of_bounds(ty, d.y_min, d.y_max)
        snap_x, snap_y = snapped[i]
        err_x = abs(snap_x - tx)
        err_y = abs(snap_y - ty)
        within = err_x <= task.tolerance_x + 1e-12 and err_y <= task.tolerance_y + 1e-12

        # 网格正中间目标在页面上有两个等距吸附值（例如 32.5 可吸附到 30 或 35），
        # 两者误差相同、均为页面可达的最优解，真值同时记录，断言按集合匹配。
        def nearest_options(v: float, step: float, lo: float, hi: float, final_v: float) -> list[float]:
            k = half_up_round(v / step)
            opts = {
                clamp(k * step, lo, hi),
                clamp((k - 1) * step, lo, hi),
                clamp(final_v, lo, hi),
            }
            return sorted(opts)

        point_truth.append(
            {
                "index": i,
                "target": [tx, ty],
                "expected_actual": [snap_x, snap_y],
                "nearest_snap_options": [
                    nearest_options(tx, d.x_grid, d.x_min, d.x_max, snap_x),
                    nearest_options(ty, d.y_grid, d.y_min, d.y_max, snap_y),
                ],
                "expected_error": [err_x, err_y],
                "out_of_bounds": {"x": oob_x, "y": oob_y},
                "mid_grid": {
                    "x": is_mid_grid(tx, d.x_grid),
                    "y": is_mid_grid(ty, d.y_grid),
                },
                "snap_reachable_within_tolerance": within and not (oob_x or oob_y),
            }
        )
        if oob_x or oob_y or not within:
            overall_structural_fail = True

    if order_bad:
        expected_status = "FAIL_ORDER"
    elif any(p["out_of_bounds"]["x"] or p["out_of_bounds"]["y"] for p in point_truth):
        expected_status = "FAIL_OUT_OF_BOUNDS"
    elif any(not p["snap_reachable_within_tolerance"] for p in point_truth):
        expected_status = "FAIL_TOLERANCE_UNREACHABLE"
    else:
        expected_status = "SUCCESS"

    return {
        "schema_version": "truth/v1",
        "task_id": task.task_id,
        "expected_status": expected_status,
        "order_violation": order_bad,
        "points": point_truth,
        "note": "本真值仅由公开领域规则（网格步长、轴域、顺序约束）离线推导，仅供测试断言；采集器禁止读取。",
    }


def write_samples() -> None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    index: list[dict[str, str]] = []
    for raw in build_samples():
        task = parse_task(raw)
        (TASKS_DIR / f"{task.task_id}.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        truth = build_truth(task)
        (TRUTH_DIR / f"{task.task_id}.truth.json").write_text(
            json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        index.append({"task_id": task.task_id, "title": task.title, "group": task.group})
    (TASKS_DIR / "index.json").write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "tasks": index}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # 真值目录声明：采集器禁入
    (TRUTH_DIR / "README.md").write_text(
        "# 独立真值（测试专用）\n\n本目录文件由 sample_gen 依据公开领域规则离线推导，"
        "仅允许测试代码读取；RPA 采集器/引擎不得访问本目录。\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    write_samples()
    print(f"样例已生成：任务 {TASKS_DIR}，真值 {TRUTH_DIR}")
