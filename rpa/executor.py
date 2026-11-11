"""RPA 执行器：用 Playwright 驱动本地流程编辑页完成拖拽重排。

关键设计：
- 每一轮都重新从 DOM 回读当前顺序，再调用 planner.plan_moves 重新规划；
  源页顺序一旦被用户改变，旧计划立即作废，不存在过期下标问题。
- 每次只执行一条移动，执行后立即回读校验；回读与预期不符则重试，
  超过重试上限如实报错。
- 固定步骤若被用户拖离槽位，重新规划会生成“拖回锚点槽位”的移动。
- 干扰钩子仅用于测试：在第 after_moves 次移动后，通过页面自身的公开
  拖拽能力模拟用户手动换位，并在事件日志中标注为“用户手动换位”。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from collector.collector import CollectionError, collect_dom_order
from core.models import Spec
from core.planner import Move, Plan, plan_moves
from core.verifier import verify_order

# 触发 HTML5 原生 dragstart/dragover/drop 的浏览器内脚本
_DRAG_JS = """
([srcId, anchorId]) => {
  const list = document.querySelector('#step-list');
  const src = list.querySelector(`li[data-step-id="${srcId}"]`);
  if (!src) return {ok: false, error: '找不到源卡片 ' + srcId};
  const dt = new DataTransfer();
  const fire = (el, type) => {
    const ev = new DragEvent(type, {bubbles: true, cancelable: true, dataTransfer: dt});
    el.dispatchEvent(ev);
  };
  fire(src, 'dragstart');
  let target;
  if (anchorId === null) {
    target = list;                       // 拖到首位：落到列表容器
  } else {
    const anchor = list.querySelector(`li[data-step-id="${anchorId}"]`);
    if (!anchor) return {ok: false, error: '找不到锚点卡片 ' + anchorId};
    target = anchor;
  }
  fire(target, 'dragover');
  fire(target, 'drop');
  fire(src, 'dragend');
  return {ok: true};
}
"""

# 页面侧手动换位：直接复用页面自己的 moveCard 公开函数（模拟用户拖放的最终效果）
_MANUAL_SWAP_JS = """
([a, b]) => {
  if (typeof window.moveCard !== 'function') return {ok: false, error: '页面未暴露 moveCard'};
  window.moveCard(a, b);
  return {ok: true};
}
"""


@dataclass
class StepRecord:
    move_no: int
    planned: dict
    order_before: list[str]
    order_after: list[str]
    replanned: bool
    note: str = ""


@dataclass
class ExecutionResult:
    ok: bool
    scenario_id: str
    target: list[str]
    final_order: list[str]
    moves_executed: int
    replans: int
    manual_swaps: list[dict] = field(default_factory=list)
    history: list[StepRecord] = field(default_factory=list)
    verification: Optional[dict] = None
    errors: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)


def execute_in_browser(
    spec: Spec,
    page_url: str,
    headless: bool = True,
    on_event: Optional[Callable[[str, dict], None]] = None,
    max_rounds_factor: int = 6,
) -> ExecutionResult:
    """打开页面并执行完整重排；返回带完整证据的 ExecutionResult。"""
    from playwright.sync_api import sync_playwright

    result = ExecutionResult(
        ok=False,
        scenario_id=spec.scenario_id,
        target=[],
        final_order=[],
        moves_executed=0,
        replans=0,
    )
    interference = spec.interference
    fired_interference = False

    def emit(kind: str, payload: dict) -> None:
        result.evidence.append({"kind": kind, **payload})
        if on_event:
            on_event(kind, payload)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(page_url, wait_until="networkidle")
            page.wait_for_selector("#step-list li[data-step-id]")

            max_rounds = max_rounds_factor * len(spec.steps)
            rounds = 0
            first_plan: Plan | None = None
            # 上一轮我们自己动作后应当出现的顺序；回读若与之不同，
            # 说明页面被外部（用户手动换位）改变，旧计划作废、计一次重规划。
            expected_order: list[str] | None = None

            while rounds < max_rounds:
                rounds += 1
                # 1) 每轮回读真实顺序（杜绝过期下标）
                current = collect_dom_order(page)
                emit("readback", {"round": rounds, "order": list(current)})

                # 2) 以最新顺序重新规划（每轮都重算，天然不持有过期下标）
                plan = plan_moves(spec, current)
                if first_plan is None:
                    first_plan = plan
                    result.target = list(plan.target)
                    emit("plan", {
                        "target": list(plan.target),
                        "lower_bound": plan.lower_bound,
                        "diagnostics": plan.diagnostics,
                    })
                elif expected_order is not None and current != expected_order:
                    # 回读顺序偏离我们自己动作的预期 -> 外部手动换位
                    result.replans += 1
                    emit("replan", {
                        "round": rounds,
                        "reason": "回读顺序偏离上一动作预期，判定页面被用户手动改变，"
                                  "旧计划作废，按最新顺序重新规划",
                        "expected_after_last_action": expected_order,
                        "actual_readback": list(current),
                        "remaining_moves": len(plan.moves),
                    })

                if not plan.moves:
                    result.final_order = current
                    break

                # 3) 测试干扰：第 after_moves 次真实移动之后模拟手动换位
                if (
                    interference
                    and not fired_interference
                    and result.moves_executed >= interference["after_moves"]
                ):
                    a, b = interference["swap_a"], interference["swap_b"]
                    swapped = page.evaluate(_MANUAL_SWAP_JS, [a, b])
                    fired_interference = True
                    if swapped.get("ok"):
                        order_now = collect_dom_order(page)
                        # 故意保留 expected_order 为旧值：下一轮回读将发现偏离
                        result.manual_swaps.append(
                            {"after_moves": result.moves_executed, "swap": [a, b],
                             "order_after_swap": order_now}
                        )
                        emit("manual_swap", {"swap": [a, b], "order": order_now})
                        continue  # 下一轮立即重新规划，不执行旧计划
                    result.errors.append(f"手动换位注入失败：{swapped.get('error')}")

                # 4) 执行本计划的第一条移动
                mv: Move = plan.moves[0]
                order_before = collect_dom_order(page)
                expected_after = _expected_order_after(order_before, mv)
                drag = page.evaluate(_DRAG_JS, [mv.step_id, mv.anchor_id])
                if not drag.get("ok"):
                    result.errors.append(f"拖拽脚本失败：{drag.get('error')}")
                    break

                # 5) 回读校验，不一致则重试一次（页面 DnD 偶发未命中）
                order_after = collect_dom_order(page)
                if order_after != expected_after:
                    emit("retry", {
                        "move": mv.step_id,
                        "expected": expected_after,
                        "actual": order_after,
                    })
                    page.evaluate(_DRAG_JS, [mv.step_id, mv.anchor_id])
                    order_after = collect_dom_order(page)
                    if order_after != expected_after:
                        result.errors.append(
                            f"拖拽 {mv.step_id} 后回读与预期不符；"
                            f"预期 {expected_after}，实际 {order_after}"
                        )
                        break

                replanned_before_move = result.replans
                result.moves_executed += 1
                expected_order = order_after  # 仅记录我们自己动作造成的顺序
                result.history.append(StepRecord(
                    move_no=result.moves_executed,
                    planned={"step_id": mv.step_id, "anchor_id": mv.anchor_id,
                             "note": mv.note},
                    order_before=order_before,
                    order_after=order_after,
                    replanned=replanned_before_move > 0,
                ))
                emit("move", {
                    "move_no": result.moves_executed,
                    "step_id": mv.step_id,
                    "anchor_id": mv.anchor_id,
                    "note": mv.note,
                    "order_after": order_after,
                })
            else:
                result.errors.append(
                    f"超过最大轮次 {max_rounds} 仍未收敛，停止执行并保留现场。"
                )

            # 6) 独立验证（不依赖计划本身）
            final_order = collect_dom_order(page)
            result.final_order = final_order
            verification = verify_order(spec, final_order)
            result.verification = verification.to_dict()
            result.ok = verification.ok and not result.errors
            emit("verify", result.verification)
            return result
        except CollectionError as exc:
            result.errors.append(f"采集错误：{exc}")
            return result
        except Exception as exc:  # noqa: BLE001 - 浏览器异常统一收口为证据
            result.errors.append(f"浏览器执行异常：{type(exc).__name__}: {exc}")
            return result
        finally:
            browser.close()


def _expected_order_after(order: list[str], mv: Move) -> list[str]:
    """纯函数推导一次移动后的期望顺序，用于与 DOM 回读比对。"""
    sim = list(order)
    sim.remove(mv.step_id)
    if mv.anchor_id is None:
        sim.insert(0, mv.step_id)
    else:
        sim.insert(sim.index(mv.anchor_id) + 1, mv.step_id)
    return sim
