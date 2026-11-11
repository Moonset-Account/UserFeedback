"""RPA 引擎：通过 Playwright 操作受控曲线编辑器页面。

核心闭环
========
1. 经公开 HTTP 接口采集任务（:mod:`curve_rpa.collector`）；
2. 打开编辑器页面，**仅依据页面公开刻度标签与像素位置反解仿射坐标映射**，
   因此天然兼容非默认缩放、平移与反向纵轴（负斜率）；
3. 结构性预检：越界目标、违反 x 严格递增的目标先给出失败分类，
   但仍执行真实浏览器操作以留存页面夹取/约束证据；
4. 逐点迭代：鼠标拖动（页面自动网格吸附）→ 读取页面显示的实际数值 →
   键盘方向键微调修正 → 再读数；达到容限或用尽最大尝试次数即止；
5. 页面叠加目标点、实际点与每次误差，保存截图与完整迭代证据；
6. 失败必须基于“页面显示数值 vs 目标值”的真实误差，绝不以鼠标到达像素代替数值正确。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

from .collector import HttpTaskCollector
from .geometry import (
    AffineMap,
    TickSample,
    clamp,
    fit_affine,
    out_of_bounds,
    snap_to_grid,
    violates_x_order,
    within_tolerance,
)
from .task_schema import Task

ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = ROOT / "data" / "runs"

# 注入编辑器页面的叠加层脚本（目标点/实际点/每次误差）
_OVERLAY_JS = r"""
(() => {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const svg = document.getElementById("chart");
  let layer = document.getElementById("rpa-overlay");
  if (!layer) {
    layer = document.createElementNS(SVG_NS, "g");
    layer.id = "rpa-overlay";
    // 叠加层绝不拦截指针事件：控制点拖动必须命中编辑器自身元素
    layer.setAttribute("pointer-events", "none");
    layer.style.pointerEvents = "none";
    // 插入到首个控制点之前，保证控制点在叠加层之上可被命中
    const firstHandle = svg.querySelector(".point-handle");
    svg.insertBefore(layer, firstHandle || null);
  }
  layer.innerHTML = "";
  const box = document.createElement("div");
  box.id = "rpa-error-panel";
  box.style.cssText = "position:absolute;right:12px;top:12px;width:270px;max-height:240px;overflow:auto;" +
    "background:rgba(10,18,30,0.92);border:1px solid #2a3a52;border-radius:8px;padding:8px 10px;" +
    "color:#e6edf6;font:12px/1.6 ui-monospace,Menlo,monospace;z-index:5;pointer-events:none;";
  const stage = document.getElementById("stage");
  const old = document.getElementById("rpa-error-panel");
  if (old) old.remove();
  stage.appendChild(box);
  window.__rpaOverlay = {
    _last: null,
    redrawLast() {
      if (this._last) this.draw(this._last.targets, this._last.actuals, this._last.errors, this._last.iteration);
    },
    draw(targets, actuals, errors, iteration) {
      this._last = { targets, actuals, errors, iteration };
      layer.innerHTML = "";
      const toXY = (p) => {
        // 复用编辑器自身的公开变换函数，保证叠加位置与页面一致
        return [window.dataToScreenX ? window.dataToScreenX(p[0]) : 0,
                window.dataToScreenY ? window.dataToScreenY(p[1]) : 0];
      };
      targets.forEach((t, i) => {
        const [tx, ty] = toXY(t);
        const cross = document.createElementNS(SVG_NS, "g");
        cross.setAttribute("class", "rpa-target");
        const s = 9;
        const l1 = document.createElementNS(SVG_NS, "line");
        l1.setAttribute("x1", tx - s); l1.setAttribute("y1", ty - s);
        l1.setAttribute("x2", tx + s); l1.setAttribute("y2", ty + s);
        l1.setAttribute("stroke", "#ff5d5d"); l1.setAttribute("stroke-width", 2);
        const l2 = document.createElementNS(SVG_NS, "line");
        l2.setAttribute("x1", tx - s); l2.setAttribute("y1", ty + s);
        l2.setAttribute("x2", tx + s); l2.setAttribute("y2", ty - s);
        l2.setAttribute("stroke", "#ff5d5d"); l2.setAttribute("stroke-width", 2);
        cross.appendChild(l1); cross.appendChild(l2);
        const tt = document.createElementNS(SVG_NS, "text");
        tt.setAttribute("x", tx + 12); tt.setAttribute("y", ty - 12);
        tt.setAttribute("fill", "#ff9c9c"); tt.setAttribute("font-size", 11);
        tt.textContent = `目标 P${i + 1}`;
        cross.appendChild(tt);
        layer.appendChild(cross);

        const a = actuals[i];
        if (a) {
          const [ax, ay] = toXY(a);
          const c = document.createElementNS(SVG_NS, "circle");
          c.setAttribute("cx", ax); c.setAttribute("cy", ay); c.setAttribute("r", 5);
          c.setAttribute("fill", "none"); c.setAttribute("stroke", "#41e0a3");
          c.setAttribute("stroke-width", 2); c.setAttribute("stroke-dasharray", "3 2");
          layer.appendChild(c);
          const at = document.createElementNS(SVG_NS, "text");
          at.setAttribute("x", ax + 10); at.setAttribute("y", ay + 16);
          at.setAttribute("fill", "#8ff0cb"); at.setAttribute("font-size", 11);
          at.textContent = `实际 P${i + 1}`;
          layer.appendChild(at);
        }
      });
      box.innerHTML = `<b>RPA 迭代 #${iteration}</b><br>` + errors.map((e) =>
        `P${e.index + 1} Δx=${e.dx.toFixed(2)} Δy=${e.dy.toFixed(2)}（${e.phase}）`).join("<br>");
    },
  };
})();
"""


@dataclass
class PointAttempt:
    iteration: int
    phase: str
    action: str
    actual: list[float]
    target: list[float]
    error: list[float]


@dataclass
class PointResult:
    index: int
    target: list[float]
    final_actual: list[float]
    final_error: list[float]
    within_tolerance: bool
    attempts: list[PointAttempt] = field(default_factory=list)
    stalled: bool = False


@dataclass
class RunResult:
    task_id: str
    status: str
    failure_reason: str | None
    preflight: dict[str, Any]
    mapping: dict[str, Any]
    point_results: list[dict[str, Any]]
    evidence: dict[str, Any]
    iterations_total: int


class CurveEditorAutomation:
    """对受控曲线编辑器执行校准点输入的 RPA。"""

    def __init__(self, base_url: str, headless: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.collector = HttpTaskCollector(base_url)

    # ---------- 公开 DOM 读取 ----------
    def _svg_origin(self, page: Page) -> tuple[float, float]:
        """SVG 画布左上角在浏览器视口中的坐标（刻度/控制点的 boundingBox 基于视口）。"""
        box = page.locator("#chart").bounding_box()
        if not box:
            raise RuntimeError("无法获取 SVG 画布位置")
        return box["x"], box["y"]

    def _read_ticks(self, page: Page, cls: str) -> list[TickSample]:
        # 刻度是 SVG <text> 元素：必须用 CSS 选择器，且用 text_content（inner_text 不支持 SVG）
        origin_x, origin_y = self._svg_origin(page)
        locators = page.locator(f".{cls}")
        count = locators.count()
        samples: list[TickSample] = []
        for i in range(count):
            node = locators.nth(i)
            text = node.text_content().strip()
            try:
                value = float(text)
            except ValueError:
                continue
            box = node.bounding_box()
            if not box:
                continue
            # 转换到 SVG 用户坐标，使仿射映射与编辑器 dataToScreen* 处于同一坐标系
            if cls == "tick-x":
                pixel = box["x"] + box["width"] / 2 - origin_x
            else:
                pixel = box["y"] + box["height"] / 2 - origin_y
            samples.append(TickSample(tick_value=value, pixel=pixel))
        # 按刻度数值排序：斜率符号因此直接表达轴方向（反向纵轴为正斜率）
        samples.sort(key=lambda s: s.tick_value)
        # 去重（同值刻度理论上不会重复，防御性处理）
        uniq: list[TickSample] = []
        seen: set[float] = set()
        for s in samples:
            if s.tick_value not in seen:
                uniq.append(s)
                seen.add(s.tick_value)
        if len(uniq) < 2:
            raise RuntimeError(f"公开刻度不足，无法建立坐标映射（{cls} 仅读到 {len(uniq)} 个）")
        return uniq

    @staticmethod
    def _read_point_values(page: Page) -> list[tuple[float, float]]:
        values: list[tuple[float, float]] = []
        rows = page.locator("#point-rows tr")
        for i in range(rows.count()):
            x = float(rows.nth(i).locator(".point-x").text_content())
            y = float(rows.nth(i).locator(".point-y").text_content())
            values.append((x, y))
        return values

    @staticmethod
    def _read_view_state(page: Page) -> dict[str, Any]:
        """从页面公开的视图状态文本解析 zoom/反向轴/网格/轴域（页面显示信息）。"""
        text = page.locator("#view-state").inner_text()
        out: dict[str, Any] = {}
        m_zoom = re.search(r"zoom=(-?[\d.]+)", text)
        if m_zoom:
            out["zoom"] = float(m_zoom.group(1))
        m_pan = re.search(r"pan=\((-?[\d.]+), (-?[\d.]+)\)", text)
        if m_pan:
            out["pan_x"] = float(m_pan.group(1))
            out["pan_y"] = float(m_pan.group(2))
        m_inv = re.search(r"inverted_y=(true|false)", text)
        out["inverted_y"] = bool(m_inv and m_inv.group(1) == "true")
        m_grid = re.search(r"grid=\((-?[\d.]+), (-?[\d.]+)\)", text)
        if m_grid:
            out["x_grid"] = float(m_grid.group(1))
            out["y_grid"] = float(m_grid.group(2))
        m_axis = re.search(r"轴域 x\[(-?[\d.]+),(-?[\d.]+)\] y\[(-?[\d.]+),(-?[\d.]+)\]", text)
        if m_axis:
            out["x_min"] = float(m_axis.group(1))
            out["x_max"] = float(m_axis.group(2))
            out["y_min"] = float(m_axis.group(3))
            out["y_max"] = float(m_axis.group(4))
        return out

    # ---------- 主流程 ----------
    def run(self, task_id: str) -> RunResult:
        task = self.collector.fetch_task(task_id)
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        evidence_dir = RUNS_DIR / task_id
        evidence_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(f"{self.base_url}/editor?task_id={task_id}", wait_until="networkidle")
            page.wait_for_selector(".point-handle", timeout=10000)

            # 暴露编辑器变换给叠加层使用
            page.evaluate(
                """() => {
                    window.dataToScreenX = dataToScreenX;
                    window.dataToScreenY = dataToScreenY;
                }"""
            )

            map_x = fit_affine(self._read_ticks(page, "tick-x"))
            map_y = fit_affine(self._read_ticks(page, "tick-y"))
            origin = self._svg_origin(page)
            view = self._read_view_state(page)
            mapping_info = {
                "x": asdict(map_x),
                "y": asdict(map_y),
                "svg_origin_viewport": {"x": origin[0], "y": origin[1]},
                "tick_count": {
                    "x": len(self._read_ticks(page, "tick-x")),
                    "y": len(self._read_ticks(page, "tick-y")),
                },
                "view_state": view,
            }

            preflight = self._preflight(task)
            page.evaluate(_OVERLAY_JS)

            point_results: list[PointResult] = []
            iterations_total = 0
            for i, target in enumerate(task.target_points):
                pr, used = self._drive_point(page, task, i, tuple(target), map_x, map_y, origin, evidence_dir)
                point_results.append(pr)
                iterations_total += used
                self._refresh_overlay(page, task, point_results, iterations_total)

            # 最后一次微调可能触发编辑器重绘并清空叠加层，截图前再同步一次全部点状态
            self._refresh_overlay(page, task, point_results, iterations_total)
            page.wait_for_timeout(150)
            screenshot = evidence_dir / "final.png"
            page.screenshot(path=str(screenshot), full_page=False)
            browser.close()

        status, reason = self._verdict(task, preflight, point_results)
        evidence = {
            "screenshot": str(screenshot.relative_to(ROOT)),
            "evidence_dir": str(evidence_dir.relative_to(ROOT)),
            "public_sources": [
                f"GET {self.base_url}/api/tasks/{task_id}",
                f"GET {self.base_url}/editor?task_id={task_id}",
                "DOM: .tick-x / .tick-y 公开刻度",
                "DOM: #point-rows 页面显示数值",
            ],
        }
        result = RunResult(
            task_id=task_id,
            status=status,
            failure_reason=reason,
            preflight=preflight,
            mapping=mapping_info,
            point_results=[self._point_to_dict(p) for p in point_results],
            evidence=evidence,
            iterations_total=iterations_total,
        )
        (evidence_dir / "result.json").write_text(
            json.dumps(self._result_to_dict(result), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    # ---------- 结构性预检 ----------
    @staticmethod
    def _preflight(task: Task) -> dict[str, Any]:
        d = task.domain
        order_bad = violates_x_order([t[0] for t in task.target_points])
        points: list[dict[str, Any]] = []
        for i, (tx, ty) in enumerate(task.target_points):
            oob_x = out_of_bounds(tx, d.x_min, d.x_max)
            oob_y = out_of_bounds(ty, d.y_min, d.y_max)
            snap_x = clamp(snap_to_grid(tx, d.x_grid), d.x_min, d.x_max)
            snap_y = clamp(snap_to_grid(ty, d.y_grid), d.y_min, d.y_max)
            reachable = (
                not oob_x
                and not oob_y
                and abs(snap_x - tx) <= task.tolerance_x + 1e-12
                and abs(snap_y - ty) <= task.tolerance_y + 1e-12
            )
            points.append(
                {
                    "index": i,
                    "target": [tx, ty],
                    "out_of_bounds": {"x": oob_x, "y": oob_y},
                    "best_snap": [snap_x, snap_y],
                    "snap_error": [abs(snap_x - tx), abs(snap_y - ty)],
                    "reachable": reachable,
                }
            )
        return {"order_violation": order_bad, "points": points}

    # ---------- 单点驱动 ----------
    def _drive_point(
        self,
        page: Page,
        task: Task,
        index: int,
        target: tuple[float, float],
        map_x: AffineMap,
        map_y: AffineMap,
        origin: tuple[float, float],
        evidence_dir: Path,
    ) -> tuple[PointResult, int]:
        attempts: list[PointAttempt] = []
        # 每轴状态：known 为 None 表示尚未试探；否则为“增大键(→/↑)是否使数据值增大”
        axis_state: dict[str, dict[str, Any]] = {
            "x": {"known": None, "before": None},
            "y": {"known": None, "before": None},
        }
        last_actual: tuple[float, float] | None = None
        stall_rounds = 0
        recent: list[tuple[float, float]] = []  # 最近读数，用于检测网格极限振荡
        best_actual: tuple[float, float] | None = None
        best_err_mag = float("inf")
        iteration = 0

        while iteration < task.max_attempts_per_point:
            iteration += 1
            actuals = self._read_point_values(page)
            actual = actuals[index]
            err = [actual[0] - target[0], actual[1] - target[1]]
            err_mag = abs(err[0]) + abs(err[1])
            if err_mag < best_err_mag - 1e-12:
                best_err_mag = err_mag
                best_actual = actual
            within = within_tolerance(actual[0], target[0], task.tolerance_x) and within_tolerance(
                actual[1], target[1], task.tolerance_y
            )
            phase = "drag" if iteration == 1 else "keyboard-nudge"
            attempts.append(
                PointAttempt(
                    iteration=iteration,
                    phase=phase,
                    action="mouse-drag" if iteration == 1 else "keyboard-arrows",
                    actual=list(actual),
                    target=list(target),
                    error=err,
                )
            )
            if within:
                return self._finalize_point(index, target, actual, err, True, attempts, False), iteration

            # 停滞检测：连续两轮页面显示值不变（被顺序/轴域约束夹住）
            if last_actual is not None and actual == last_actual:
                stall_rounds += 1
            else:
                stall_rounds = 0
            if stall_rounds >= 2:
                break

            # 振荡检测：网格正中间目标会在两个相邻吸附值间来回，误差无法再下降，
            # 出现周期 2 的循环即判定已达页面物理极限，停止微调，保留最优读数用于裁决
            recent.append(actual)
            if len(recent) >= 4 and recent[-1] == recent[-3] and recent[-2] == recent[-4]:
                attempts[-1].phase = "keyboard-nudge(oscillation-break)"
                break
            recent = recent[-4:]

            # 用上一轮读数验证试探键的真实作用方向（兼容反向纵轴，不预设键位含义）
            if last_actual is not None:
                self._learn_key_sign("x", last_actual[0], actual[0], axis_state["x"])
                self._learn_key_sign("y", last_actual[1], actual[1], axis_state["y"])
            last_actual = actual

            if iteration == 1:
                # 首步：依据公开刻度映射算出目标 SVG 像素，换算回视口坐标后真实拖动
                target_px = map_x.to_pixel(target[0]) + origin[0]
                target_py = map_y.to_pixel(target[1]) + origin[1]
                self._drag_point(page, index, target_px, target_py)
            else:
                # 后续：键盘微调，逐轴“试探 → 验证 → 单向逼近”，每轮只按一次键以防越过目标
                self._nudge_point(page, task, index, actual, target, axis_state)

            self._refresh_overlay(
                page,
                task,
                [PointResult(index, list(target), list(actual), err, False, attempts, False)],
                iteration,
            )

        # 结束后重新读取页面值；若迭代中出现过更优读数（振荡场景），以最优读数作为最终证据
        page_actual = self._read_point_values(page)[index]
        actual = best_actual if best_actual is not None and (
            abs(best_actual[0] - target[0]) + abs(best_actual[1] - target[1])
            < abs(page_actual[0] - target[0]) + abs(page_actual[1] - target[1]) - 1e-12
        ) else page_actual
        err = [actual[0] - target[0], actual[1] - target[1]]
        within = within_tolerance(actual[0], target[0], task.tolerance_x) and within_tolerance(
            actual[1], target[1], task.tolerance_y
        )
        stalled = stall_rounds >= 2 or (len(recent) >= 4 and recent[-1] == recent[-3])
        return self._finalize_point(index, target, actual, err, within, attempts, stalled), iteration

    def _drag_point(self, page: Page, index: int, target_px: float, target_py: float) -> None:
        handle = page.locator(f".point-handle[data-index='{index}'] circle")
        box = handle.bounding_box()
        if not box:
            raise RuntimeError(f"找不到控制点 P{index + 1}")
        sx, sy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(sx, sy)
        page.mouse.down()
        # 分两段移动，更接近真人拖动，页面在 pointerup 时执行吸附
        page.mouse.move((sx + target_px) / 2, (sy + target_py) / 2, steps=8)
        page.mouse.move(target_px, target_py, steps=12)
        page.mouse.up()
        page.wait_for_timeout(120)

    def _nudge_point(
        self,
        page: Page,
        task: Task,
        index: int,
        actual: tuple[float, float],
        target: tuple[float, float],
        axis_state: dict[str, dict[str, Any]],
    ) -> None:
        # force=True 绕过叠加层命中测试；键盘焦点只与 DOM 选中态有关
        page.locator(f".point-handle[data-index='{index}']").click(force=True)
        d = task.domain
        self._nudge_axis(page, "x", actual[0], target[0], d.x_grid, axis_state["x"])
        self._nudge_axis(page, "y", actual[1], target[1], d.y_grid, axis_state["y"])
        page.wait_for_timeout(80)

    @staticmethod
    def _nudge_axis(
        page: Page,
        axis: str,
        actual_v: float,
        target_v: float,
        grid: float,
        state: dict[str, Any],
    ) -> None:
        if abs(target_v - actual_v) < 1e-9:
            return
        inc_key = "ArrowRight" if axis == "x" else "ArrowUp"
        dec_key = "ArrowLeft" if axis == "x" else "ArrowDown"

        if state["known"] is None:
            # 试探：只按一次增大键，记录试探前数值，下一轮学习真实方向
            state["before"] = actual_v
            page.keyboard.press(inc_key)
            return

        # 已学会键位：只朝目标方向每次移动一个网格单位，永不越过目标
        want_increase = target_v > actual_v
        use_inc = want_increase == state["known"]
        page.keyboard.press(inc_key if use_inc else dec_key)

    @staticmethod
    def _learn_key_sign(axis: str, before: float, after: float, state: dict[str, Any]) -> None:
        """依据试探按键前后页面显示的真实数值变化，确定增大键的作用方向。

        上一轮按的是“增大键”（→/↑）。数值变大 => 增大键确实增大数据值；
        变小 => 方向相反；不变 => 被页面约束夹住，保持未知等待下一次可观测变化。
        """
        if state["known"] is not None or state["before"] is None:
            return
        delta = after - state["before"]
        if abs(delta) < 1e-12:
            return
        state["known"] = delta > 0
        state["before"] = None

    # ---------- 叠加层 ----------
    def _refresh_overlay(
        self,
        page: Page,
        task: Task,
        point_results: list[PointResult],
        iteration: int,
    ) -> None:
        targets = [list(t) for t in task.target_points]
        # 未处理点从页面实时读取当前值，保证叠加层任何时刻都显示全部点的目标/实际
        page_values = self._read_point_values(page)
        actuals: list[list[float]] = [[x, y] for x, y in page_values]
        errors: list[dict[str, Any]] = []
        known = {pr.index: pr for pr in point_results}
        for i, target in enumerate(task.target_points):
            a = actuals[i]
            if i in known:
                pr = known[i]
                errors.append(
                    {
                        "index": i,
                        "dx": pr.final_error[0],
                        "dy": pr.final_error[1],
                        "phase": (pr.attempts[-1].phase if pr.attempts else "init"),
                    }
                )
            else:
                errors.append(
                    {
                        "index": i,
                        "dx": a[0] - target[0],
                        "dy": a[1] - target[1],
                        "phase": "pending",
                    }
                )
        page.evaluate(
            """({targets, actuals, errors, iteration}) =>
                window.__rpaOverlay.draw(targets, actuals, errors, iteration)""",
            {"targets": targets, "actuals": actuals, "errors": errors, "iteration": iteration},
        )

    # ---------- 最终裁决 ----------
    @staticmethod
    def _finalize_point(
        index: int,
        target: tuple[float, float],
        actual: tuple[float, float],
        err: list[float],
        within: bool,
        attempts: list[PointAttempt],
        stalled: bool,
    ) -> PointResult:
        return PointResult(
            index=index,
            target=list(target),
            final_actual=list(actual),
            final_error=err,
            within_tolerance=within,
            attempts=attempts,
            stalled=stalled,
        )

    @staticmethod
    def _verdict(task: Task, preflight: dict[str, Any], results: list[PointResult]) -> tuple[str, str | None]:
        if preflight["order_violation"]:
            return "FAIL_ORDER", "目标点 x 不满足严格递增，页面阻止控制点交叉，约束不可满足"
        if any(p["out_of_bounds"]["x"] or p["out_of_bounds"]["y"] for p in preflight["points"]):
            bad = [
                f"P{p['index'] + 1}"
                for p in preflight["points"]
                if p["out_of_bounds"]["x"] or p["out_of_bounds"]["y"]
            ]
            return "FAIL_OUT_OF_BOUNDS", f"目标越界（{', '.join(bad)}），页面只能夹取到轴域边界"
        if any(not p.within_tolerance for p in results):
            worst = max(results, key=lambda p: max(abs(p.final_error[0]), abs(p.final_error[1])))
            return (
                "FAIL_TOLERANCE_UNREACHABLE",
                f"达到最大尝试次数后 P{worst.index + 1} 误差 {worst.final_error} 仍超出容限 "
                f"(x≤{task.tolerance_x}, y≤{task.tolerance_y})",
            )
        return "SUCCESS", None

    @staticmethod
    def _point_to_dict(pr: PointResult) -> dict[str, Any]:
        return {
            "index": pr.index,
            "target": pr.target,
            "final_actual": pr.final_actual,
            "final_error": pr.final_error,
            "within_tolerance": pr.within_tolerance,
            "stalled": pr.stalled,
            "attempts": [asdict(a) for a in pr.attempts],
        }

    @staticmethod
    def _result_to_dict(r: RunResult) -> dict[str, Any]:
        return {
            "task_id": r.task_id,
            "status": r.status,
            "failure_reason": r.failure_reason,
            "preflight": r.preflight,
            "mapping": r.mapping,
            "point_results": r.point_results,
            "evidence": r.evidence,
            "iterations_total": r.iterations_total,
        }
