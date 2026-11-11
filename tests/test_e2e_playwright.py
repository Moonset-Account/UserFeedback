"""端到端验证：真实 Chromium 执行采集→规划→录入→回读全链路。

这些测试会实际启动浏览器，断言：
- 可写格全部成功、缺失格跳过、隐藏/只读格拒绝、无任何 failed；
- 回读显示值与类型与独立真值一致；
- 两个演示小样例浏览器总耗时在 15 秒内。
"""
from __future__ import annotations

import time

import pytest

from rpa_grid.automation import GridAutomation
from rpa_grid.collector import collect
from rpa_grid.planner import plan_execution

DEMO_SCENARIOS = ["demo-normal", "demo-edge"]
ALL_SCENARIOS = ["demo-normal", "demo-edge", "acceptance-full"]


def _expected_counts(truth_data):
    success = skipped = rejected = 0
    for c in truth_data["cells"]:
        if not c["writable"]:
            rejected += 1
        elif c["kind"] == "missing":
            skipped += 1
        else:
            success += 1
    return success, rejected, skipped


@pytest.mark.parametrize("scenario", ALL_SCENARIOS)
def test_full_run_matches_truth(server, truth, scenario):
    expected = truth(scenario)
    exp_success, exp_rejected, exp_skipped = _expected_counts(expected)

    bundle = collect(server, scenario)
    plan = plan_execution(bundle.matrix, bundle.grid_config)
    result = GridAutomation(base_url=server, headless=True).run(
        plan, bundle.matrix, bundle.grid_config, bundle.evidence)

    assert result.failed == 0, [
        o.detail for o in result.outcomes if o.status == "failed"]
    assert result.succeeded == exp_success
    assert result.rejected == exp_rejected
    assert result.skipped == exp_skipped
    assert result.ok is True  # 无 failed 即整体通过（rejected 是预期保真拒绝）

    # 逐格回读核对：成功/跳过格的回读必须与真值一致
    by_coord = {(c["row"], c["column"]): c for c in expected["cells"]}
    for oc in result.outcomes:
        cell = by_coord[(oc.row_index, oc.column_key)]
        if oc.status == "rejected":
            assert cell["writable"] is False
        elif oc.status == "skipped":
            assert cell["kind"] == "missing"
            assert oc.actual_kind == "missing"
        else:
            assert oc.actual_text == cell["raw_text"]
            assert oc.actual_kind == cell["kind"]


def test_two_demo_scenarios_within_15_seconds(server):
    total = 0
    for scenario in DEMO_SCENARIOS:
        bundle = collect(server, scenario)
        plan = plan_execution(bundle.matrix, bundle.grid_config)
        started = time.perf_counter()
        result = GridAutomation(base_url=server, headless=True).run(
            plan, bundle.matrix, bundle.grid_config, bundle.evidence)
        total += time.perf_counter() - started
        assert result.failed == 0
    assert total < 15, f"两个演示场景浏览器总耗时 {total:.1f}s 超过 15s"


def test_leading_zero_and_formula_fidelity(server):
    """前导零与公式样式字符串的浏览器级保真专项。"""
    bundle = collect(server, "demo-edge")
    plan = plan_execution(bundle.matrix, bundle.grid_config)
    result = GridAutomation(base_url=server, headless=True).run(
        plan, bundle.matrix, bundle.grid_config, bundle.evidence)
    outcomes = {(o.row_index, o.column_key): o for o in result.outcomes}

    id_cell = outcomes[(0, "样本编码")]
    assert id_cell.actual_text == "0001" and id_cell.actual_kind == "id_text"

    formula_cell = outcomes[(0, "审核状态")]
    assert formula_cell.actual_text == "=1+1"
    assert formula_cell.actual_kind == "text"  # 未被求值成数字 2

    dde_cell = outcomes[(1, "备注")]
    assert dde_cell.actual_text == "=cmd|' /C notepad'!A1"
    assert dde_cell.actual_kind == "text"


def test_multiline_and_cr_fidelity(server):
    """含制表符、换行、裸回车、转义引号的文本逐字符保真。"""
    bundle = collect(server, "demo-edge")
    plan = plan_execution(bundle.matrix, bundle.grid_config)
    result = GridAutomation(base_url=server, headless=True).run(
        plan, bundle.matrix, bundle.grid_config, bundle.evidence)
    outcomes = {(o.row_index, o.column_key): o for o in result.outcomes}

    assert outcomes[(0, "备注")].actual_text == "批次A\t第一行\n第二行"
    assert outcomes[(2, "备注")].actual_text == '含"引号"与\r回车的文本'


def test_readonly_cell_rejected_with_explanation(server):
    """只读格必须被拒绝且带解释，且不掩盖其他格的成功。"""
    bundle = collect(server, "demo-edge")
    plan = plan_execution(bundle.matrix, bundle.grid_config)
    result = GridAutomation(base_url=server, headless=True).run(
        plan, bundle.matrix, bundle.grid_config, bundle.evidence)
    ro = [o for o in result.outcomes
          if o.row_index == 1 and o.column_key == "审核状态"][0]
    assert ro.status == "rejected"
    assert "只读" in ro.detail
    # 同任务其他格仍成功
    assert result.succeeded > 0
