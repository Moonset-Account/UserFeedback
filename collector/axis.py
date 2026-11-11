"""轴与刻度识别、文本配对、线性/对数轴标定。

全部依据画布上可见的短线（刻度）与文本（刻度标签），不读取任何隐藏数据。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from . import geometry
from .svg_model import RenderedNode

# 判定刻度短线的长度范围（画布像素）
TICK_MIN_LEN = 3.0
TICK_MAX_LEN = 24.0
# 标签与刻度末端的最大配对距离（像素）
LABEL_MATCH_RADIUS = 28.0
# 线性/对数拟合残差明显劣化倍数
LOG_PREFER_RATIO = 10.0
# 认为是对数轴所需的最少刻度数
MIN_TICKS_FOR_LOG = 3
# 标签里允许的数字格式（不含 10^n 上标形式，属子集外）
_NUMBER_RE = re.compile(
    r"[-+]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.?\d*|\.\d+)"
    r"(?:[eE][-+]?\d+)?"
)


class CalibrationError(Exception):
    """尺度无法确定，拒绝给出假精确值。"""


@dataclass
class Tick:
    base: tuple[float, float]      # 靠轴一端（画布坐标）
    tip: tuple[float, float]       # 朝外一端
    axis_coord: float = 0.0        # 基点在轴线上的投影坐标
    label_text: str | None = None
    label_anchor: tuple | None = None  # 文本锚点（画布坐标）
    value: float | None = None
    parse_note: str | None = None


@dataclass
class AxisCalibration:
    orientation: str               # "x" / "y"
    scale: str                     # "linear" / "log"
    k: float                       # data = k * coord + b（log 轴时 k 乘 log10）
    b: float
    ticks: list
    direction: int                 # +1 数据随画布坐标增大；-1 反向轴
    residual: float                # 拟合 RMSE（数据单位）
    axis_line: object = None       # (p1, p2) 画布坐标
    domain_min: float = 0.0        # 已标定刻度覆盖的画布坐标范围
    domain_max: float = 0.0
    log_base: float = 10.0
    notes: list[str] = field(default_factory=list)

    def project(self, px: float, py: float) -> float:
        return _project_to_axis(px, py, self.axis_line, self.orientation)

    def check_domain(self, coord: float):
        """刻度范围之外的点不允许外推，拒绝假精确。"""
        margin = 2.0  # 像素级容差
        if coord < self.domain_min - margin or coord > self.domain_max + margin:
            raise CalibrationError(
                f"{self.orientation.upper()} 坐标 {coord:.2f}px 超出全部已标定"
                f"刻度覆盖范围 [{self.domain_min:.1f}, {self.domain_max:.1f}]px，"
                "无法用可见刻度验证，拒绝外推给值"
            )

    def to_data(self, coord: float) -> tuple[float, float, str]:
        """返回 (值, 该点换算说明, 警告或空串)。"""
        self.check_domain(coord)
        if self.scale == "log":
            logv = self.k * coord + self.b
            if logv <= 0:
                raise CalibrationError(
                    f"对数值非法：log10(value)={logv:.4g} <= 0，"
                    "画布坐标落在正对数范围之外，无法恢复真实值"
                )
            return self.log_base ** logv, logv, ""
        v = self.k * coord + self.b
        return v, v, ""

    def describe(self, coord: float) -> str:
        if self.scale == "log":
            logv = self.k * coord + self.b
            if logv <= 0:
                return (f"{coord:.2f}px -> log10(v)={logv:.4g}（非法，"
                        "真实对数轴上不会出现该位置的数据）")
            return (f"{coord:.2f}px -> log10(v) = {self.k:.6g}×{coord:.2f}"
                    f" + {self.b:.6g} = {logv:.6g} -> v = "
                    f"{self.log_base}^({logv:.4g}) = {self.log_base ** logv:.6g}")
        v = self.k * coord + self.b
        return (f"{coord:.2f}px -> v = {self.k:.6g}×{coord:.2f}"
                f" + {self.b:.6g} = {v:.6g}")


# ------------------------------------------------------------------
# 文本数字解析
# ------------------------------------------------------------------
def parse_label_number(text: str) -> tuple[float | None, str | None]:
    """从刻度标签文本解析数字。

    支持：整数/小数、千分位、科学计数法、百分号（按 /100 处理）、
    常见单位前后缀（仅剥离，不做量纲换算）。
    不支持：10^n 上标、分数、日期等。
    """
    if text is None:
        return None, "空标签"
    cleaned = text.strip().replace(" ", "")
    if not cleaned:
        return None, "空标签"
    if "%" in cleaned:
        core = cleaned.replace("%", "")
        m = _NUMBER_RE.fullmatch(core)
        if not m:
            return None, f"百分号标签无法解析: {text!r}"
        return float(m.group(0).replace(",", "")) / 100.0, None
    # 剥离常见单位前后缀（仅本协议子集内的剥离，不做换算）
    candidate = cleaned
    for unit in ("ms", "Hz", "kHz", "MHz", "kg", "mm", "cm", "km",
                 "mL", "mol", "Pa", "kPa", "V", "mV", "A", "W", "s"):
        if candidate.endswith(unit):
            candidate = candidate[: -len(unit)]
            break
    for prefix in ("~", "≈"):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix):]
    m = _NUMBER_RE.fullmatch(candidate)
    if not m:
        return None, f"标签不是支持的数字格式（不支持 10^n 上标等）: {text!r}"
    return float(m.group(0).replace(",", "")), None


# ------------------------------------------------------------------
# 刻度候选与轴线
# ------------------------------------------------------------------
def _segment_canvas(node: RenderedNode) -> list[tuple[float, float]]:
    return [geometry.apply(node.ctm, x, y) for x, y in node.points]


def find_axis_line(nodes: list[RenderedNode], orientation: str):
    """在候选直线中选出“附着刻度短线最多”的一条作为轴线。

    网格线与轴线同为水平/垂直线，区别在于只有轴线上附着刻度短线；
    先比附着刻度数，相同再比线长。
    """
    candidates = []
    for n in nodes:
        if n.hidden or n.tag != "line" or len(n.points) != 2:
            continue
        p1, p2 = _segment_canvas(n)
        if orientation == "x" and not _nearly(p1[1], p2[1], 2.0):
            continue
        if orientation == "y" and not _nearly(p1[0], p2[0], 2.0):
            continue
        length = geometry.segment_length(p1[0], p1[1], p2[0], p2[1])
        if length < 80:
            continue
        candidates.append((p1, p2, length))
    if not candidates:
        return None

    def tick_count(p1, p2):
        count = 0
        for n in nodes:
            if n.hidden or n.tag != "line" or len(n.points) != 2:
                continue
            q1, q2 = _segment_canvas(n)
            tlen = geometry.segment_length(q1[0], q1[1], q2[0], q2[1])
            if not (TICK_MIN_LEN <= tlen <= TICK_MAX_LEN):
                continue
            if orientation == "x" and not _nearly(q1[0], q2[0], 2.5):
                continue
            if orientation == "y" and not _nearly(q1[1], q2[1], 2.5):
                continue
            d1 = geometry.point_line_distance(q1[0], q1[1], *p1, *p2)
            d2 = geometry.point_line_distance(q2[0], q2[1], *p1, *p2)
            if min(d1, d2) <= 2.5:
                count += 1
        return count

    scored = [(tick_count(p1, p2), length, p1, p2)
              for p1, p2, length in candidates]
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    _, _, p1, p2 = scored[0]
    return p1, p2


def _nearly(value, target, tol):
    return abs(value - target) <= tol


def _project_to_axis(px, py, axis_line, orientation):
    """把点投影到轴线，返回沿轴坐标（像素）。"""
    p1, p2 = axis_line
    if orientation == "x":
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        denom = dx * dx + dy * dy
        if denom < 1e-12:
            return px
        t = ((px - p1[0]) * dx + (py - p1[1]) * dy) / denom
        return p1[0] + t * dx
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    denom = dx * dx + dy * dy
    if denom < 1e-12:
        return py
    t = ((px - p1[0]) * dx + (py - p1[1]) * dy) / denom
    return p1[1] + t * dy


def find_ticks(nodes: list[RenderedNode], orientation: str, axis_line):
    """找出一端落在轴线上的短线作为刻度，记录沿轴投影坐标。"""
    p1, p2 = axis_line
    ticks = []
    for n in nodes:
        if n.hidden or n.tag != "line" or len(n.points) != 2:
            continue
        q1, q2 = _segment_canvas(n)
        length = geometry.segment_length(q1[0], q1[1], q2[0], q2[1])
        if not (TICK_MIN_LEN <= length <= TICK_MAX_LEN):
            continue
        if orientation == "x" and not _nearly(q1[0], q2[0], 2.5):
            continue
        if orientation == "y" and not _nearly(q1[1], q2[1], 2.5):
            continue
        d1 = geometry.point_line_distance(q1[0], q1[1], *p1, *p2)
        d2 = geometry.point_line_distance(q2[0], q2[1], *p1, *p2)
        if min(d1, d2) > 2.5:
            continue  # 两端都不靠轴，不是该轴的刻度
        if d1 <= d2:
            base, tip = q1, q2
        else:
            base, tip = q2, q1
        tick = Tick(base=base, tip=tip)
        tick.axis_coord = _project_to_axis(
            base[0], base[1], axis_line, orientation
        )
        ticks.append(tick)
    return ticks


def find_texts(nodes: list[RenderedNode]) -> list[RenderedNode]:
    return [n for n in nodes if n.tag == "text" and not n.hidden and n.text]


def pair_labels(ticks: list[Tick], texts: list[RenderedNode],
                orientation: str, axis_line) -> list[Tick]:
    """把可见文本与刻度配对：取离刻度末端最近且落在轴外侧的文本。"""
    used = set()
    # 轴外侧：x 轴标签一般在轴下方，y 轴标签一般在轴左侧
    for tick in ticks:
        best_idx, best_d = None, LABEL_MATCH_RADIUS
        for i, t in enumerate(texts):
            if i in used:
                continue
            anchor = geometry.apply(t.ctm, *t.text_origin)
            if orientation == "x":
                # 文本锚点在刻度末端附近，且水平对齐刻度
                dx = abs(anchor[0] - tick.base[0])
                dy = anchor[1] - tick.tip[1]
                if dy < -2:  # 跑到轴线另一侧，多半不是该轴标签
                    continue
                d = math.hypot(dx, max(dy, 0))
            else:
                dy = abs(anchor[1] - tick.base[1])
                dx = tick.tip[0] - anchor[0]
                if dx < -2:
                    continue
                d = math.hypot(max(dx, 0), dy)
            if d < best_d:
                best_d, best_idx = d, i
        if best_idx is not None:
            t = texts[best_idx]
            used.add(best_idx)
            tick.label_text = t.text
            tick.label_anchor = geometry.apply(t.ctm, *t.text_origin)
    return ticks


# ------------------------------------------------------------------
# 标定拟合
# ------------------------------------------------------------------
def _fit_linear(coords, values) -> tuple[float, float, float]:
    """最小二乘 value = k*coord + b，返回 k,b,RMSE。"""
    n = len(coords)
    sx = sum(coords)
    sy = sum(values)
    sxx = sum(c * c for c in coords)
    sxy = sum(c * v for c, v in zip(coords, values))
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-18:
        raise CalibrationError("刻度坐标全部重合，无法拟合轴尺度")
    k = (n * sxy - sx * sy) / denom
    b = (sy - k * sx) / n
    rmse = math.sqrt(
        sum((v - (k * c + b)) ** 2 for c, v in zip(coords, values)) / n
    )
    return k, b, rmse


def calibrate(nodes: list[RenderedNode], orientation: str,
              extra_notes: list[str] | None = None) -> AxisCalibration:
    axis_line = find_axis_line(nodes, orientation)
    if axis_line is None:
        raise CalibrationError(
            f"未找到{'水平' if orientation == 'x' else '垂直'}轴线"
            "（或轴线上没有可识别的刻度），无法确定该维度的尺度"
        )
    ticks = find_ticks(nodes, orientation, axis_line)
    texts = find_texts(nodes)
    ticks = pair_labels(ticks, texts, orientation, axis_line)
    labeled = []
    for t in ticks:
        if t.label_text is None:
            continue
        value, note = parse_label_number(t.label_text)
        t.parse_note = note
        if value is None:
            continue
        t.value = value
        labeled.append(t)
    notes = list(extra_notes or [])
    if len(labeled) < 2:
        raise CalibrationError(
            f"{orientation.upper()} 轴只有 {len(labeled)} 个可解析的带标签刻度，"
            "至少需要 2 个才能确定线性尺度，拒绝猜测"
        )
    coords = [t.axis_coord for t in labeled]
    values = [t.value for t in labeled]

    # 退化保护：刻度像素坐标全部重合，或标签数值全部相同，
    # 都无法确定“单位像素对应多少数据”，拒绝猜测
    coord_span = max(coords) - min(coords)
    value_span_raw = max(values) - min(values)
    if coord_span < 1e-9:
        raise CalibrationError(
            f"{orientation.upper()} 轴 {len(labeled)} 个刻度的画布投影完全重合，"
            "无法确定尺度，拒绝猜测"
        )
    if value_span_raw == 0:
        raise CalibrationError(
            f"{orientation.upper()} 轴 {len(labeled)} 个刻度标签的数值完全相同"
            f"（均为 {values[0]:g}），斜率为零无法换算，拒绝给出假精确值"
        )

    # 线性拟合（默认模型）
    k_lin, b_lin, rmse_lin = _fit_linear(coords, values)
    scale = "linear"
    k, b, rmse = k_lin, b_lin, rmse_lin
    value_span = value_span_raw
    rel_rmse = rmse_lin / value_span if value_span else rmse_lin

    # 对数判定：需要 ≥3 个正标签且对数拟合显著更好
    positive = [v for v in values if v > 0]
    if len(labeled) >= MIN_TICKS_FOR_LOG and len(positive) == len(values):
        logs = [math.log10(v) for v in values]
        k_log, b_log, rmse_log_abs = _fit_linear(coords, logs)
        # 把对数拟合误差换算回数值空间以便同尺度比较
        rmse_log_val = math.sqrt(sum(
            (v - 10 ** (k_log * c + b_log)) ** 2
            for c, v in zip(coords, values)
        ) / len(values))
        if rmse_log_val * LOG_PREFER_RATIO <= max(rmse_lin, 1e-15):
            scale, k, b, rmse = "log", k_log, b_log, rmse_log_abs
            notes.append(
                f"对数模型残差 {rmse_log_val:.3g} 显著优于线性模型 "
                f"{rmse_lin:.3g}，判定为以 10 为底的对数轴"
            )
        else:
            notes.append(
                f"线性模型残差 {rmse_lin:.3g}，对数模型 {rmse_log_val:.3g}，"
                "采用线性轴"
            )
    elif any(v <= 0 for v in values):
        notes.append("刻度标签存在非正值，对数轴不适用，采用线性轴")
    else:
        notes.append("带标签刻度不足 3 个，无法可靠区分对数轴，按线性轴处理")

    if scale == "linear" and rel_rmse > 0.02:
        notes.append(
            f"警告：线性拟合相对残差 {rel_rmse:.2%} 偏大，"
            "刻度可能并非等距，结果不确定性增大"
        )

    direction = 1 if k > 0 else -1
    return AxisCalibration(
        orientation=orientation, scale=scale, k=k, b=b, ticks=labeled,
        direction=direction, residual=rmse, axis_line=axis_line,
        domain_min=min(coords), domain_max=max(coords),
        notes=notes,
    )
