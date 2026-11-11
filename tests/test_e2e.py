"""端到端测试：Playwright 真实浏览器拖拽 + 每步回读 + 手动换位重规划。

覆盖验收要求：
- 正常场景真实拖拽成功，独立验证全部依赖与固定位置；
- 执行中手动换位后，RPA 重新采集、重新规划并收敛；
- 不可满足场景不启动浏览器，直接返回具体冲突链；
- 完整验收场景（10 步 + 干扰）跑通。
"""
import pytest

from collector.collector import collect_dom_order
from core.planner import UnsatisfiableError, check_satisfiable
from core.verifier import verify_order
from rpa.runner import run_demo_pair, run_scenario

pytestmark = pytest.mark.e2e


def test_normal_scenario_real_drag(live_server, load_spec):
    record = run_scenario("normal-titration", base_url=live_server, headless=True)
    assert record["status"] == "success", record["errors"]
    exec_ = record["execution"]
    assert exec_["moves_executed"] >= 1
    # 每一步都留有移动前/回读后两份顺序证据
    assert all("order_before" in h and "order_after" in h for h in exec_["history"])
    # 独立验证三项全过
    assert exec_["verification"]["ok"] is True
    assert all(c["ok"] for c in exec_["verification"]["checks"])
    # 固定锚点位置独立复核
    spec = load_spec("normal-titration")
    for sid, slot in spec.fixed.items():
        assert exec_["final_order"][slot] == sid


def test_manual_swap_triggers_replan(live_server, load_spec):
    record = run_scenario("manual-swap", base_url=live_server, headless=True)
    assert record["status"] == "success", record["errors"]
    exec_ = record["execution"]
    assert exec_["manual_swaps"], "必须记录到一次用户手动换位"
    assert exec_["replans"] >= 1, "手动换位后必须发生重新规划"
    # 最终顺序仍通过独立验证（含固定 P1 在槽位 0）
    assert exec_["verification"]["ok"] is True
    spec = load_spec("manual-swap")
    assert verify_order(spec, exec_["final_order"]).ok


def test_unsatisfiable_scenario_reports_chain_without_browser(live_server):
    record = run_scenario("cyclic-deps", base_url=live_server, mode="check")
    assert record["status"] == "unsatisfiable"
    assert record["execution"] is None
    text = " ".join(record["conflict_chains"])
    assert "循环依赖" in text
    # 固定冲突场景同样给出具体链
    record2 = run_scenario("fixed-conflict", base_url=live_server, mode="check")
    assert record2["status"] == "unsatisfiable"
    assert any("槽位 2" in c for c in record2["conflict_chains"])


def test_full_acceptance_with_interference(live_server):
    record = run_scenario("full-acceptance", base_url=live_server, headless=True)
    assert record["status"] == "success", record["errors"]
    exec_ = record["execution"]
    assert exec_["verification"]["ok"] is True
    assert exec_["final_order"][0] == "F01"
    assert exec_["final_order"][9] == "F10"
    assert exec_["manual_swaps"] and exec_["replans"] >= 1


def test_demo_pair_finishes_quickly(live_server):
    demo = run_demo_pair(base_url=live_server, headless=True)
    assert demo["normal_status"] == "success"
    assert demo["boundary_status"] == "unsatisfiable"
    assert demo["elapsed_seconds"] <= 20  # 机器上留余量；页面目标为 15 秒


def test_dom_readback_matches_visible_cards(live_server):
    """直接操作浏览器：采集器读到的顺序与页面 li 顺序一致。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(f"{live_server}/source/normal-titration", wait_until="networkidle")
            page.wait_for_selector("#step-list li")
            order = collect_dom_order(page)
            visible = page.eval_on_selector_all(
                "#step-list li", "els => els.map(e => e.dataset.stepId)"
            )
            assert order == visible
            # 页面公开 moveCard 后回读立即变化
            page.evaluate("window.moveCard('S3', null)")
            assert collect_dom_order(page)[0] == "S3"
        finally:
            browser.close()
