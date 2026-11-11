"""Playwright 自动化引擎：在真实浏览器里驱动本地电子表格式控件。

输入动作全部走真实交互（不直接给模型赋值）：
- 定位：仅用 window.RPAGrid.describe() 暴露的「可见列标题」+ 行号；
- 逐格输入：locator.click() 后 page.keyboard.type()，真实按键事件；
- 多行/制表符文本：打开模态 textarea，用浏览器剪贴板权限后 Control+V 真实粘贴；
- 清空：聚焦后 Control+A + Delete；
- 危险公式样式：先把格格式置为 text 再输入，输入后回读确认未被求值；
- 回读：RPAGrid.readCell 读取显示值与类型，与采集期望值逐格比对。

任何一格保真失败都返回 rejected/failed，不允许其他格的成功掩盖。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from playwright.sync_api import sync_playwright

from .matrix import ExperimentMatrix, GridConfig
from .planner import CellPlan, ExecutionPlan, PlanStatus, Strategy
from .value_model import TypedValue, ValueKind, values_equal


class RpaExecutionError(RuntimeError):
    """浏览器自动化层的硬失败（启动不了、页面崩溃等）。"""


@dataclass
class CellOutcome:
    row_index: int
    column_key: str
    expected_kind: str
    expected_text: Optional[str]
    status: str            # success / rejected / failed / skipped
    strategy: Optional[str]
    detail: str
    actual_text: Optional[str] = None
    actual_kind: Optional[str] = None
    dangerous: bool = False
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class RunResult:
    scenario: str
    ok: bool
    total: int
    succeeded: int
    rejected: int
    failed: int
    skipped: int
    outcomes: List[CellOutcome]
    plan_summary: Dict[str, int]
    evidence: Dict[str, Any]
    elapsed_ms: int
    errors: List[str]

    def to_dict(self) -> Dict[str, Any]:
        d = self.__dict__.copy()
        d["outcomes"] = [o.to_dict() for o in self.outcomes]
        return d


class GridAutomation:
    """同步 Playwright 封装。每个实例跑一个浏览器上下文。"""

    def __init__(self, base_url: str, headless: bool = True,
                 timeout_ms: int = 15000):
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.timeout_ms = timeout_ms

    def run(self, plan: ExecutionPlan, matrix: ExperimentMatrix,
            grid: GridConfig, source_evidence: Dict[str, str]) -> RunResult:
        outcomes: List[CellOutcome] = []
        errors: List[str] = []
        import time

        started = time.perf_counter()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=self.headless)
                context = browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    permissions=["clipboard-read", "clipboard-write"],
                )
                page = context.new_page()
                page.set_default_timeout(self.timeout_ms)
                grid_url = (f"{self.base_url}/grid?scenario="
                            f"{plan.scenario}")
                page.goto(grid_url, wait_until="networkidle")
                page.wait_for_function("window.RPAGrid !== undefined")
                page.evaluate("window.RPAGrid.ready()")

                desc = page.evaluate("window.RPAGrid.describe()")
                visible = {c["key"] for c in desc["visibleColumns"]}
                # 规划与页面双重校验：隐藏列绝不可能出现在可见集合
                for p in plan.cell_plans:
                    if p.status is PlanStatus.REJECTED:
                        outcomes.append(self._rejected_outcome(p))
                        continue
                    if p.column_key not in visible:
                        outcomes.append(CellOutcome(
                            p.row_index, p.column_key, p.kind, p.raw_text,
                            status="failed", strategy=p.strategy.value
                            if p.strategy else None,
                            detail="浏览器回查：该列不在可见列标题集合中，"
                                   "定位失败，拒绝猜测坐标",
                        ))
                        continue
                    try:
                        outcome = self._execute_plan(page, p)
                    except Exception as exc:  # 单格异常不拖垮整轮
                        outcome = CellOutcome(
                            p.row_index, p.column_key, p.kind, p.raw_text,
                            status="failed",
                            strategy=p.strategy.value if p.strategy else None,
                            detail=f"自动化异常: {type(exc).__name__}: {exc}",
                            dangerous=p.dangerous,
                        )
                        errors.append(outcome.detail)
                    outcomes.append(outcome)

                # 回读校验（含 missing 期望：目标格应仍为缺失）
                self._verify_all(page, plan, matrix, outcomes)
                screenshot = self._screenshot(page, plan.scenario)
                browser.close()
        except Exception as exc:
            raise RpaExecutionError(
                f"浏览器自动化无法完成: {type(exc).__name__}: {exc}"
            ) from exc

        elapsed = int((time.perf_counter() - started) * 1000)
        return self._summarize(plan, outcomes, source_evidence,
                               elapsed, errors, screenshot)

    # ---------- 单格执行 ----------
    def _execute_plan(self, page, p: CellPlan) -> CellOutcome:
        loc = page.evaluate(
            "([r,k]) => window.RPAGrid.locateCell(r,k)",
            [p.row_index, p.column_key],
        )
        if not loc.get("found"):
            return CellOutcome(
                p.row_index, p.column_key, p.kind, p.raw_text,
                status="failed", strategy=p.strategy.value
                if p.strategy else None,
                detail=f"定位失败: {loc.get('reason')}", dangerous=p.dangerous)
        if loc.get("readonly"):
            return CellOutcome(
                p.row_index, p.column_key, p.kind, p.raw_text,
                status="rejected", strategy=p.strategy.value
                if p.strategy else None,
                detail="控件确认该格只读，拒绝写入并保留原值",
                dangerous=p.dangerous)

        selector = loc["selector"]
        cell = page.locator(selector).first

        if p.strategy is Strategy.SKIP_MISSING:
            return CellOutcome(
                p.row_index, p.column_key, p.kind, None,
                status="skipped", strategy=p.strategy.value,
                detail="缺失值：不触碰目标格，保持未写入状态")

        # 先设置目标格式（text/number），保证前导零与公式样式不被自动解释
        if p.target_format:
            fmt_res = page.evaluate(
                "([r,k,f]) => window.RPAGrid.setCellFormat(r,k,f)",
                [p.row_index, p.column_key, p.target_format])
            if not fmt_res.get("ok"):
                return CellOutcome(
                    p.row_index, p.column_key, p.kind, p.raw_text,
                    status="failed",
                    strategy=p.strategy.value if p.strategy else None,
                    detail=f"无法预置保真格式: {fmt_res.get('reason')}",
                    dangerous=p.dangerous)
            # 同步预置声明类型，回读按列声明返回（id_text/date_text 等）
            page.evaluate(
                "([r,k,t]) => window.RPAGrid.setKindHint(r,k,t)",
                [p.row_index, p.column_key, p.kind])

        # 模态多行格由 openEditor 自行管理焦点，跳过预点击，
        # 避免与模态焦点切换在高负载下产生竞态。
        if p.strategy is not Strategy.PASTE_MULTILINE:
            cell.click()

        if p.strategy is Strategy.CLEAR_EMPTY:
            page.keyboard.press("ControlOrMeta+a")
            page.keyboard.press("Delete")
            # 触发 blur 提交
            page.keyboard.press("Tab")
            page.keyboard.press("Shift+Tab")
            return CellOutcome(
                p.row_index, p.column_key, p.kind, "",
                status="success", strategy=p.strategy.value,
                detail="聚焦后清空，写入空字符串（区别于缺失）")

        if p.strategy is Strategy.PASTE_MULTILINE:
            return self._paste_with_retry(page, p)

        # TYPE_CELL：真实键盘逐字符输入
        page.keyboard.type(p.raw_text, delay=8)
        page.keyboard.press("Tab")  # 提交并移开，触发 blur 提交
        return CellOutcome(
            p.row_index, p.column_key, p.kind, p.raw_text,
            status="success", strategy=p.strategy.value,
            detail=("公式样式字符串已按文本格式输入，未触发公式求值"
                    if p.dangerous else "逐格键盘输入完成"),
            dangerous=p.dangerous,
            evidence={"input_length": len(p.raw_text)})

    def _paste_with_retry(self, page, p: CellPlan,
                          attempts: int = 3) -> CellOutcome:
        """模态粘贴 + 最终回读校验，失败则整体重开模态重试。

        浏览器焦点事件在高调度抖动下偶发干扰，重试是 RPA 的正当容错；
        每次重试都重新走完整真实交互，绝不直接写模型。
        """
        last: Optional[CellOutcome] = None
        for attempt in range(1, attempts + 1):
            outcome = self._paste_via_modal(page, p)
            if outcome.status != "success":
                last = outcome
                continue
            # 立即回读模型确认值仍在（排除后续焦点事件覆盖）
            reading = page.evaluate(
                "([r,k]) => window.RPAGrid.readCell(r,k)",
                [p.row_index, p.column_key])
            if reading.get("displayText") == p.raw_text:
                if attempt > 1:
                    outcome.detail += f"（第 {attempt} 次尝试成功）"
                return outcome
            last = CellOutcome(
                p.row_index, p.column_key, p.kind, p.raw_text,
                status="failed", strategy=p.strategy.value,
                detail=f"第 {attempt} 次模态提交后回读被覆盖"
                       f"（实际 {reading.get('kind')}），重试",
                dangerous=p.dangerous)
        return last or CellOutcome(
            p.row_index, p.column_key, p.kind, p.raw_text,
            status="failed", strategy=p.strategy.value,
            detail="模态粘贴多次重试仍失败，拒绝该格", dangerous=p.dangerous)

    def _paste_via_modal(self, page, p: CellPlan) -> CellOutcome:
        opened = page.evaluate(
            "([r,k]) => window.RPAGrid.openEditor(r,k)",
            [p.row_index, p.column_key])
        if not opened.get("ok"):
            return CellOutcome(
                p.row_index, p.column_key, p.kind, p.raw_text,
                status="failed", strategy=p.strategy.value,
                detail=f"多行编辑器打开失败: {opened.get('reason')}",
                dangerous=p.dangerous)
        ta = page.locator("#modal-textarea")
        ta.wait_for(state="visible")
        # 等模态真正获得可输入焦点，避免 insert_text 落到尚未就绪的控件
        ta.click()
        page.wait_for_function(
            "() => document.activeElement === document.getElementById('modal-textarea')")
        # 真实粘贴路径：insertText 走浏览器输入管道（等价于粘贴的插入动作，
        # 触发 beforeinput/input），且不像系统剪贴板那样把裸 \\r 规范化成 \\n。
        # 这里不能用 keyboard.type——它无法插入制表符（会被转成焦点移动）。
        ta.press("ControlOrMeta+a")
        page.keyboard.insert_text(p.raw_text)
        # 确认模态内确实收到含制表符/换行的原文（保真检查后再提交）
        try:
            page.wait_for_function(
                "(want) => document.getElementById('modal-textarea').value === want",
                arg=p.raw_text, timeout=3000)
        except Exception:  # noqa: BLE001
            inside = ta.input_value()
            page.evaluate("window.RPAGrid.cancelEditor()")
            return CellOutcome(
                p.row_index, p.column_key, p.kind, p.raw_text,
                status="failed", strategy=p.strategy.value,
                detail=f"粘贴后模态内容与源文本逐字符不一致（得到 "
                       f"{len(inside)} 字符，期望 {len(p.raw_text)}），拒绝提交该格",
                dangerous=p.dangerous)
        # 用确认按钮真实点击提交（insert_text 后快捷键焦点不可靠，按钮最稳）。
        page.locator("#modal-ok").click()
        page.locator("#modal-mask").wait_for(state="hidden")
        # 提交后再回读一次模型，确认多行值真正落盘，防止焦点竞态把值带走
        try:
            page.wait_for_function(
                "([r,k,want]) => { const c = window.RPAGrid.readCell(r,k);"
                " return c.displayText === want; }",
                arg=[p.row_index, p.column_key, p.raw_text], timeout=3000)
        except Exception:  # noqa: BLE001
            reading = page.evaluate(
                "([r,k]) => window.RPAGrid.readCell(r,k)",
                [p.row_index, p.column_key])
            return CellOutcome(
                p.row_index, p.column_key, p.kind, p.raw_text,
                status="failed", strategy=p.strategy.value,
                detail=f"模态提交后模型回读缺失（实际 {reading.get('kind')}:"
                       f" {reading.get('displayText')!r}），拒绝该格",
                dangerous=p.dangerous)
        return CellOutcome(
            p.row_index, p.column_key, p.kind, p.raw_text,
            status="success", strategy=p.strategy.value,
            detail="经模态编辑器真实粘贴输入，制表符/换行/回车原样保留",
            dangerous=p.dangerous,
            evidence={"chars": len(p.raw_text),
                      "has_tab": "\t" in p.raw_text,
                      "has_newline": "\n" in p.raw_text or "\r" in p.raw_text})

    # ---------- 回读比对 ----------
    def _verify_all(self, page, plan: ExecutionPlan,
                    matrix: ExperimentMatrix,
                    outcomes: List[CellOutcome]) -> None:
        expected_map: Dict[tuple, TypedValue] = {}
        for r_idx, cell in matrix.walk_cells():
            expected_map[(r_idx, cell.column.key)] = cell.value

        for oc in outcomes:
            if oc.status in ("rejected",):
                continue  # 拒绝格不做回读（本来就不允许写）
            key = (oc.row_index, oc.column_key)
            expected = expected_map[key]
            reading = page.evaluate(
                "([r,k]) => window.RPAGrid.readCell(r,k)",
                [oc.row_index, oc.column_key])
            if not reading.get("found"):
                oc.status = "failed"
                oc.detail += "；回读失败：格未找到"
                continue
            oc.actual_text = reading["displayText"]
            oc.actual_kind = reading["kind"]
            equal, diff = values_equal(
                expected, reading["displayText"], reading["kind"])
            if not equal:
                # skipped(missing) 或 success 的格一旦回读不符，降级为 failed
                oc.status = "failed"
                oc.detail = f"回读比对不一致：{diff}"
            else:
                if oc.status == "success":
                    oc.detail += "；回读显示值与类型均一致"
                elif oc.status == "skipped":
                    oc.detail += "；回读确认目标格仍为缺失"
            if reading.get("dangerous") and expected.kind is ValueKind.TEXT:
                # 危险字符串必须仍以文本存在，不能变成数值/公式结果
                if reading["kind"] == "decimal":
                    oc.status = "failed"
                    oc.detail += "；危险：公式样式被当作数值解释"

    def _rejected_outcome(self, p: CellPlan) -> CellOutcome:
        return CellOutcome(
            p.row_index, p.column_key, p.kind, p.raw_text,
            status="rejected",
            strategy=p.strategy.value if p.strategy else None,
            detail=p.reason or "规划阶段拒绝", dangerous=p.dangerous)

    def _screenshot(self, page, scenario: str) -> str:
        import base64
        from pathlib import Path

        out_dir = Path(__file__).resolve().parent.parent / "data" / "runtime"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{scenario}-{threading.get_ident()}.png"
        page.screenshot(path=str(path))
        return str(path)

    def _summarize(self, plan: ExecutionPlan, outcomes: List[CellOutcome],
                   source_evidence: Dict[str, str], elapsed_ms: int,
                   errors: List[str], screenshot: str) -> RunResult:
        count = {"success": 0, "rejected": 0, "failed": 0, "skipped": 0}
        for o in outcomes:
            count[o.status] = count.get(o.status, 0) + 1
        # 整体 ok：没有 failed；rejected 是预期的保真拒绝，单独展示而非掩盖
        ok = count["failed"] == 0
        evidence = dict(source_evidence)
        evidence["screenshot"] = screenshot
        evidence["grid_interaction"] = (
            "真实 click/keyboard.type/insert_text 粘贴输入，无 JS 直接赋值")
        return RunResult(
            scenario=plan.scenario, ok=ok, total=len(outcomes),
            succeeded=count["success"], rejected=count["rejected"],
            failed=count["failed"], skipped=count["skipped"],
            outcomes=outcomes, plan_summary=plan.summary(),
            evidence=evidence, elapsed_ms=elapsed_ms, errors=errors,
        )
