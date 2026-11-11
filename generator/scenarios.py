"""虚构 SVG 实验图场景与独立真值生成。

重要边界：本模块是“样例生成器”，采集器永远不导入本模块、不读取真值文件；
真值 JSON 仅由测试侧用于断言采集结果。
"""

from __future__ import annotations

import math
import random

from .svg_build import STYLE, SvgBuilder

WIDTH, HEIGHT = 960, 600
PLOT = (110, 70, 760, 440)          # x, y, w, h
TICK_LEN = 8

# 固定随机种子，保证样例可复现（数据本身完全虚构）
RNG = random.Random(20261111)


# ------------------------------------------------------------------
# 通用绘图助手
# ------------------------------------------------------------------
def _linear_map(v, vmin, vmax, pmin, pmax, reverse=False):
    t = (v - vmin) / (vmax - vmin)
    if reverse:
        t = 1 - t
    return pmin + t * (pmax - pmin)


def _draw_frame(b: SvgBuilder, x_ticks, y_ticks, x_rev=False, y_rev=True,
                x_log=False, y_log=False, title="虚构实验图",
                plot=PLOT, x_label_rotate=0):
    x0, y0, w, h = plot
    b.text(WIDTH / 2, 34, title, text_anchor="middle", **STYLE["title"])
    # 网格 + 轴线
    for v, _ in x_ticks:
        px = _linear_map(math.log10(v) if x_log else v,
                         *(_log_bounds(x_ticks) if x_log else _bounds(x_ticks)),
                         x0, x0 + w, x_rev)
        b.line(px, y0, px, y0 + h, **STYLE["grid"])
    for v, _ in y_ticks:
        py = _linear_map(math.log10(v) if y_log else v,
                         *(_log_bounds(y_ticks) if y_log else _bounds(y_ticks)),
                         y0 + h, y0, y_rev)
        b.line(x0, py, x0 + w, py, **STYLE["grid"])
    b.line(x0, y0 + h, x0 + w, y0 + h, **STYLE["axis"])
    b.line(x0, y0, x0, y0 + h, **STYLE["axis"])
    # 刻度短线与标签
    for v, label in x_ticks:
        px = _linear_map(math.log10(v) if x_log else v,
                         *(_log_bounds(x_ticks) if x_log else _bounds(x_ticks)),
                         x0, x0 + w, x_rev)
        b.line(px, y0 + h, px, y0 + h + TICK_LEN, **STYLE["tick"])
        if x_label_rotate:
            b.text(px, y0 + h + 20, label, text_anchor="end",
                   transform=f"rotate({x_label_rotate} {px:g} "
                             f"{y0 + h + 14:g})",
                   **STYLE["label"])
        else:
            b.text(px, y0 + h + 26, label, text_anchor="middle",
                   **STYLE["label"])
    for v, label in y_ticks:
        py = _linear_map(math.log10(v) if y_log else v,
                         *(_log_bounds(y_ticks) if y_log else _bounds(y_ticks)),
                         y0 + h, y0, y_rev)
        b.line(x0 - TICK_LEN, py, x0, py, **STYLE["tick"])
        b.text(x0 - 12, py + 4, label, text_anchor="end", **STYLE["label"])


def _bounds(ticks):
    vals = [v for v, _ in ticks]
    return min(vals), max(vals)


def _log_bounds(ticks):
    logs = [math.log10(v) for v, _ in ticks]
    return min(logs), max(logs)


def _project(vx, vy, x_ticks, y_ticks, x_rev, y_rev, x_log, y_log,
             plot=PLOT):
    x0, y0, w, h = plot
    if x_log:
        lo, hi = _log_bounds(x_ticks)
        px = _linear_map(math.log10(vx), lo, hi, x0, x0 + w, x_rev)
    else:
        px = _linear_map(vx, *_bounds(x_ticks), x0, x0 + w, x_rev)
    if y_log:
        lo, hi = _log_bounds(y_ticks)
        py = _linear_map(math.log10(vy), lo, hi, y0 + h, y0, y_rev)
    else:
        py = _linear_map(vy, *_bounds(y_ticks), y0 + h, y0, y_rev)
    return px, py


def _ticks(vmin, vmax, step, fmt="{:g}".format):
    n = round((vmax - vmin) / step)
    return [(vmin + i * step, fmt(vmin + i * step)) for i in range(n + 1)]


def _truth(points, x_ticks, y_ticks, x_rev, y_rev, x_log, y_log,
           extra=None, plot=PLOT):
    rows = []
    for p in points:
        px, py = _project(p["x"], p["y"], x_ticks, y_ticks,
                          x_rev, y_rev, x_log, y_log, plot)
        row = {
            "x": p["x"], "y": p["y"],
            "canvas": {"x": round(px, 3), "y": round(py, 3)},
            "visible": p.get("visible", True),
            "segment": p.get("segment", 0),
        }
        if "note" in p:
            row["note"] = p["note"]
        rows.append(row)
    out = {
        "axis": {
            "x": {"scale": "log" if x_log else "linear",
                  "reversed": x_rev},
            "y": {"scale": "log" if y_log else "linear",
                  "reversed": y_rev},
        },
        "points": rows,
    }
    if extra:
        out.update(extra)
    return out


def _draw_polyline(b, points, x_ticks, y_ticks, x_rev, y_rev, x_log, y_log,
                   gap_indices=(), group_transform=None, markers=True,
                   extra_attrs=None, plot=PLOT):
    if group_transform:
        b.open_group(transform=group_transform)
    segments, cur = [], []
    for i, p in enumerate(points):
        if i in gap_indices:
            if cur:
                segments.append(cur)
            cur = []
        px, py = _project(p["x"], p["y"], x_ticks, y_ticks,
                          x_rev, y_rev, x_log, y_log, plot)
        cur.append((px, py))
    if cur:
        segments.append(cur)
    if len(segments) == 1:
        attrs = {**STYLE["series"], **(extra_attrs or {})}
        b.polyline(segments[0], **attrs)
        draw = segments[0]
    else:
        # 多段用 path 的 M/L 表达断点
        d = " ".join(
            "M " + " L ".join(f"{x:.2f} {y:.2f}" for x, y in seg)
            for seg in segments
        )
        attrs = {**STYLE["series"], **(extra_attrs or {})}
        b.path(d, **attrs)
        draw = [pt for seg in segments for pt in seg]
    if markers:
        for px, py in draw:
            b.circle(px, py, 4.5, **STYLE["marker"])
    if group_transform:
        b.close_group()


# ------------------------------------------------------------------
# 场景 1：线性轴基线（X、Y 均线性，Y 轴视觉反向属正常情形）
# ------------------------------------------------------------------
def linear_basic():
    b = SvgBuilder(WIDTH, HEIGHT)
    x_ticks = _ticks(0, 100, 10)
    y_ticks = _ticks(0, 50, 5)
    points = [{"x": x, "y": round(20 + 0.25 * x + RNG.uniform(-2, 2), 3)}
              for x in range(5, 100, 10)]
    _draw_frame(b, x_ticks, y_ticks, title="场景1 线性轴基线（虚构）")
    _draw_polyline(b, points, x_ticks, y_ticks, False, True, False, False)
    return b.build(), _truth(points, x_ticks, y_ticks, False, True, False, False)


# ------------------------------------------------------------------
# 场景 2：Y 对数轴
# ------------------------------------------------------------------
def log_y():
    b = SvgBuilder(WIDTH, HEIGHT)
    x_ticks = _ticks(0, 80, 10)
    y_vals = [1, 10, 100, 1000, 10000]
    y_ticks = [(v, f"{v:g}") for v in y_vals]
    points = [{"x": x,
               "y": round(10 ** (0.04 * x + RNG.uniform(-0.05, 0.05)), 4)}
              for x in range(5, 80, 10)]
    _draw_frame(b, x_ticks, y_ticks, y_log=True,
                title="场景2 Y轴对数刻度（虚构）")
    _draw_polyline(b, points, x_ticks, y_ticks, False, True, False, True)
    return b.build(), _truth(points, x_ticks, y_ticks, False, True, False, True)


# ------------------------------------------------------------------
# 场景 3：X、Y 双对数轴
# ------------------------------------------------------------------
def log_log():
    b = SvgBuilder(WIDTH, HEIGHT)
    x_ticks = [(v, f"{v:g}") for v in (1, 10, 100, 1000)]
    y_ticks = [(v, f"{v:g}") for v in (1, 10, 100, 1000, 10000)]
    points = [{"x": 10 ** (0.1 * i + 0.3),
               "y": round(10 ** (0.42 * i + 0.2 + RNG.uniform(-0.04, 0.04)), 4)}
              for i in range(9)]
    _draw_frame(b, x_ticks, y_ticks, x_log=True, y_log=True,
                title="场景3 双对数轴（虚构）")
    _draw_polyline(b, points, x_ticks, y_ticks, False, True, True, True)
    return b.build(), _truth(points, x_ticks, y_ticks, False, True, True, True)


# ------------------------------------------------------------------
# 场景 4：X 轴反向（数值从右向左增大）
# ------------------------------------------------------------------
def reversed_x():
    b = SvgBuilder(WIDTH, HEIGHT)
    x_ticks = _ticks(0, 60, 10)
    y_ticks = _ticks(0, 30, 5)
    points = [{"x": x, "y": round(6 + 0.3 * x + RNG.uniform(-1.2, 1.2), 3)}
              for x in range(0, 61, 10)]
    _draw_frame(b, x_ticks, y_ticks, x_rev=True,
                title="场景4 X轴反向（虚构，浓度从右向左升高）")
    _draw_polyline(b, points, x_ticks, y_ticks, True, True, False, False)
    return b.build(), _truth(points, x_ticks, y_ticks, True, True, False, False)


# ------------------------------------------------------------------
# 场景 5：嵌套 transform（三层 <g>：平移 → 缩放 → 再平移）
# ------------------------------------------------------------------
def nested_transform():
    b = SvgBuilder(WIDTH, HEIGHT)
    x_ticks = _ticks(0, 100, 20)
    y_ticks = _ticks(0, 40, 10)
    points = [{"x": x, "y": round(12 + 0.2 * x + RNG.uniform(-1.5, 1.5), 3)}
              for x in range(10, 100, 15)]
    # 三层嵌套：translate(24,-16) -> scale(0.92) -> translate(-10,12)
    chain = ("translate(24,-16)", "scale(0.92)", "translate(-10,12)")
    b.text(WIDTH / 2, 34,
           "场景5 嵌套 transform（虚构，三层 g 平移+缩放）",
           text_anchor="middle", **STYLE["title"])
    for t in chain:
        b.open_group(transform=t)
    _draw_frame(b, x_ticks, y_ticks, title="")
    _draw_polyline(b, points, x_ticks, y_ticks, False, True, False, False)
    for _ in chain:
        b.close_group()

    # 真值：先求“未变换”画布坐标，再用同一矩阵链变换
    # T1(24,-16) * S(0.92) * T2(-10,12) => a=d=0.92, e=14.8, f=-4.96
    a, b_, c, d = 0.92, 0.0, 0.0, 0.92
    e, f = 24 + 0.92 * (-10), -16 + 0.92 * 12

    def warp(px, py):
        return a * px + c * py + e, b_ * px + d * py + f

    truth = _truth(points, x_ticks, y_ticks, False, True, False, False)
    for row in truth["points"]:
        wx, wy = warp(row["canvas"]["x"], row["canvas"]["y"])
        row["canvas"]["x"] = round(wx, 3)
        row["canvas"]["y"] = round(wy, 3)
    truth["transform_chain"] = list(chain)
    truth["transform_matrix"] = [a, b_, c, d, e, f]
    return b.build(), truth


# ------------------------------------------------------------------
# 场景 6：折线断点（缺失测量，M 移笔 + 大缺口）
# ------------------------------------------------------------------
def broken_line():
    b = SvgBuilder(WIDTH, HEIGHT)
    x_ticks = _ticks(0, 120, 15)
    y_ticks = _ticks(0, 60, 10)
    points = [{"x": x,
               "y": round(15 + 0.32 * x + RNG.uniform(-2, 2), 3),
               "segment": 0 if x <= 45 else (1 if x <= 75 else 2)}
              for x in range(0, 121, 10)]
    gap = {i for i, p in enumerate(points) if p["segment"] !=
           (points[i - 1]["segment"] if i else 0)}
    _draw_frame(b, x_ticks, y_ticks,
                title="场景6 折线断点（虚构，缺失三段测量）")
    _draw_polyline(b, points, x_ticks, y_ticks, False, True, False, False,
                   gap_indices=gap)
    return b.build(), _truth(
        points, x_ticks, y_ticks, False, True, False, False,
        extra={"expected_break_count": len(gap)},
    )


# ------------------------------------------------------------------
# 场景 7：裁剪区域（部分点被 clipPath 裁掉，屏幕不可见）
# ------------------------------------------------------------------
def clipped():
    b = SvgBuilder(WIDTH, HEIGHT)
    x_ticks = _ticks(0, 100, 10)
    y_ticks = _ticks(0, 80, 10)
    points = [{"x": x, "y": round(70 - 0.55 * x + RNG.uniform(-2, 2), 3)}
              for x in range(0, 101, 8)]
    _draw_frame(b, x_ticks, y_ticks,
                title="场景7 裁剪区域（虚构，窗口外点不可见）")
    x0, y0, w, h = PLOT
    # 裁剪窗口只保留绘图区中部，两侧点被裁掉
    clip_x, clip_y = x0 + 180, y0 + 40
    clip_w, clip_h = w - 360, h - 80
    b.clip_path("window", clip_x, clip_y, clip_w, clip_h)
    b.open_group(clip_path="url(#window)")
    _draw_polyline(b, points, x_ticks, y_ticks, False, True, False, False)
    b.close_group()
    # 用真值几何判断哪些点落在窗口外
    truth = _truth(points, x_ticks, y_ticks, False, True, False, False)
    for row in truth["points"]:
        cx, cy = row["canvas"]["x"], row["canvas"]["y"]
        inside = (clip_x <= cx <= clip_x + clip_w
                  and clip_y <= cy <= clip_y + clip_h)
        row["visible"] = inside
        if not inside:
            row["note"] = "被 clipPath 裁掉，应只登记不采集"
    truth["clip_window"] = {"x": clip_x, "y": clip_y,
                            "w": clip_w, "h": clip_h}
    return b.build(), truth


# ------------------------------------------------------------------
# 场景 8：隐藏数据诱饵（display:none 的第二系列，采集器必须无视）
# ------------------------------------------------------------------
def hidden_decoys():
    b = SvgBuilder(WIDTH, HEIGHT)
    x_ticks = _ticks(0, 100, 10)
    y_ticks = _ticks(0, 50, 5)
    points = [{"x": x, "y": round(18 + 0.22 * x + RNG.uniform(-1.5, 1.5), 3)}
              for x in range(5, 100, 10)]
    decoys = [{"x": x, "y": 48.0} for x in range(5, 100, 10)]
    _draw_frame(b, x_ticks, y_ticks,
                title="场景8 隐藏诱饵（虚构，红线不可见）")
    _draw_polyline(b, points, x_ticks, y_ticks, False, True, False, False)
    # 诱饵：display:none，且把真值写在私有属性里——采集器一律不得使用
    b.open_group(id="hidden-batch", **STYLE["hidden"])
    decoy_pts = [_project(p["x"], p["y"], x_ticks, y_ticks,
                          False, True, False, False) for p in decoys]
    b.polyline(decoy_pts, data_secret_values="48,48,48",
               fill="none", stroke="#888", stroke_width=2)
    b.close_group()
    return b.build(), _truth(
        points, x_ticks, y_ticks, False, True, False, False,
        extra={"hidden_decoys": decoys,
               "expect_ignored_hidden_points": len(decoys)},
    )


# ------------------------------------------------------------------
# 场景 9：刻度不足（只有一个带标签刻度，应拒绝假精确）
# ------------------------------------------------------------------
def insufficient_ticks():
    b = SvgBuilder(WIDTH, HEIGHT)
    x0, y0, w, h = PLOT
    b.text(WIDTH / 2, 34, "场景9 刻度不足（虚构）", text_anchor="middle",
           **STYLE["title"])
    # 完整轴线，但 X 轴只画一个带标签刻度
    b.line(x0, y0 + h, x0 + w, y0 + h, **STYLE["axis"])
    b.line(x0, y0, x0, y0 + h, **STYLE["axis"])
    b.line(x0, y0 + h, x0, y0 + h + TICK_LEN, **STYLE["tick"])
    b.text(x0, y0 + h + 26, "0", text_anchor="middle", **STYLE["label"])
    # Y 轴正常
    y_ticks = _ticks(0, 40, 10)
    for v, label in y_ticks:
        py = _linear_map(v, 0, 40, y0 + h, y0)
        b.line(x0 - TICK_LEN, py, x0, py, **STYLE["tick"])
        b.text(x0 - 12, py + 4, label, text_anchor="end", **STYLE["label"])
    # 系列照样存在，但 X 维度无法标定
    pts = [(x0 + 80 * i, y0 + h - 60 * i - 30) for i in range(1, 7)]
    b.polyline(pts, **STYLE["series"])
    for px, py in pts:
        b.circle(px, py, 4.5, **STYLE["marker"])
    truth = {
        "axis": {"x": {"scale": "unknown", "reversed": False},
                 "y": {"scale": "linear", "reversed": True}},
        "points": [{"canvas": {"x": round(px, 3), "y": round(py, 3)},
                    "x": None, "visible": True} for px, py in pts],
        "expected_error_keyword": "至少需要 2 个",
    }
    return b.build(), truth


# ------------------------------------------------------------------
# 场景 10：非法对数位置（数据点画到对数轴零刻度以下的画布位置）
# ------------------------------------------------------------------
def illegal_log():
    b = SvgBuilder(WIDTH, HEIGHT)
    x_ticks = _ticks(0, 80, 10)
    y_vals = [1, 10, 100, 1000]
    y_ticks = [(v, f"{v:g}") for v in y_vals]
    _draw_frame(b, x_ticks, y_ticks, y_log=True,
                title="场景10 非法对数位置（虚构，点落在零基线以下）")
    # 正常点
    good = [{"x": x, "y": 10 ** (0.035 * x + 0.2)} for x in (10, 25, 40)]
    _draw_polyline(b, good, x_ticks, y_ticks, False, True, False, True)
    # 异常点：直接画在 Y=0 基线以下（对数轴上不存在的位置）
    x0, y0, w, h = PLOT
    bad_canvas = (x0 + 520, y0 + h + 34)
    b.circle(*bad_canvas, 5, fill="#f59e0b", stroke="#7c2d12")
    b.text(bad_canvas[0] + 12, bad_canvas[1] + 4,
           "异常读数（位置非法）", **STYLE["label"])
    truth = _truth(good, x_ticks, y_ticks, False, True, False, True)
    truth["points"].append({
        "x": None, "y": None,
        "canvas": {"x": round(bad_canvas[0], 3),
                   "y": round(bad_canvas[1], 3)},
        "visible": True,
        "note": "对数轴零基线以下，应报告非法对数值并拒绝给数",
        "expected_error_keyword": "对数",
    })
    return b.build(), truth


# ------------------------------------------------------------------
# 场景 11：viewBox + 旋转刻度标签（坐标换算的额外考验）
# ------------------------------------------------------------------
def viewBox_rotated_labels():
    inner_w, inner_h = 480, 300
    b = SvgBuilder(960, 600, viewBox=f"0 0 {inner_w} {inner_h}")
    small_plot = (60, 36, 380, 220)
    x_ticks = _ticks(0, 100, 20)
    y_ticks = _ticks(0, 50, 10)
    points = [{"x": x,
               "y": round(22 + 0.2 * x + RNG.uniform(-1.5, 1.5), 3)}
              for x in range(10, 100, 12)]
    _draw_frame(b, x_ticks, y_ticks,
                title="场景11 viewBox 与旋转标签（虚构）",
                plot=small_plot, x_label_rotate=-35)
    _draw_polyline(b, points, x_ticks, y_ticks, False, True, False, False,
                   plot=small_plot)
    truth = _truth(points, x_ticks, y_ticks, False, True, False, False,
                   plot=small_plot)
    # 真值画布坐标换算到 960x600 外显坐标（等比 meet，居中偏移为 0）
    scale = min(960 / inner_w, 600 / inner_h)
    for row in truth["points"]:
        row["canvas"]["x"] = round(row["canvas"]["x"] * scale, 3)
        row["canvas"]["y"] = round(row["canvas"]["y"] * scale, 3)
    truth["viewBox_scale"] = scale
    return b.build(), truth


SCENARIOS = {
    "linear_basic": ("场景1 线性轴基线", linear_basic),
    "log_y": ("场景2 Y对数轴", log_y),
    "log_log": ("场景3 双对数轴", log_log),
    "reversed_x": ("场景4 X反向轴", reversed_x),
    "nested_transform": ("场景5 嵌套transform", nested_transform),
    "broken_line": ("场景6 折线断点", broken_line),
    "clipped": ("场景7 裁剪区域", clipped),
    "hidden_decoys": ("场景8 隐藏诱饵", hidden_decoys),
    "insufficient_ticks": ("场景9 刻度不足", insufficient_ticks),
    "illegal_log": ("场景10 非法对数位置", illegal_log),
    "viewbox_rotated": ("场景11 viewBox旋转标签", viewBox_rotated_labels),
}
