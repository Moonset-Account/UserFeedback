"""可见数据系列提取：折线、路径（M/L/H/V/Z 子集）、圆/小矩形标记。

- 只处理可见节点；display:none 等隐藏节点在建模阶段已排除
- 裁剪：阶段内矩形并集、嵌套阶段交集；协议外裁剪按不可信跳过
- 断点：polyline 的 M 移笔、显式缺失（NaN 生成的 M）产生独立分段
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from . import geometry
from .svg_model import RenderedNode
from .transforms import UnsupportedFeature

# 判定为“标记”而不是“数据条/装饰”的圆半径、矩形边长上限（画布像素）
MARKER_MAX_RADIUS = 14.0
MARKER_MAX_SIDE = 18.0
# 折线两点视为同一点的容差（画布像素）
DEDUP_TOL = 0.6
# 路径数字与命令
_PATH_TOKEN_RE = re.compile(
    r"[MLHVCSQTAZmlhvcsqtaz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
)


@dataclass
class RawPoint:
    x: float
    y: float
    source_tag: str
    source_id: str | None
    transform_chain: list
    kind: str                 # "vertex" | "marker"
    clipped: bool = False
    clip_detail: str | None = None
    segment_index: int = 0    # 断点后的分段编号


@dataclass
class Series:
    source_id: str | None
    source_tag: str
    points: list = field(default_factory=list)       # RawPoint
    breaks: list = field(default_factory=list)       # 断点说明
    skipped: list = field(default_factory=list)      # 跳过原因（协议外裁剪等）


def _clipped(stages, x: float, y: float) -> tuple[bool, str | None]:
    """阶段间取交集；阶段内任一矩形包含即可。无裁剪阶段 => 不裁剪。"""
    if not stages:
        return False, None
    for idx, stage in enumerate(stages):
        inside_any = any(r.contains(x, y) for r in stage)
        if not inside_any:
            return True, f"被第 {idx + 1} 层 clipPath 裁掉"
    return False, None


def parse_path_d(d: str) -> list:
    """解析路径为 [(cmd, [(x,y), ...]), ...]。

    支持子集 M/L/H/V/Z（含小写相对形式）。
    遇到 C/S/Q/T/A 等曲线命令抛出 UnsupportedFeature。
    """
    tokens = _PATH_TOKEN_RE.findall(d)
    subpaths, current = [], []
    cursor = None
    start = None
    i = 0
    cmd = None
    while i < len(tokens):
        tok = tokens[i]
        if tok.isalpha():
            cmd = tok
            i += 1
            if cmd in ("C", "c", "S", "s", "Q", "q", "T", "t", "A", "a"):
                raise UnsupportedFeature(f"路径命令 {cmd}（曲线/弧线）不在支持子集内")
            continue
        if cmd is None:
            raise UnsupportedFeature("路径数据在命令字母前出现数字")
        if cmd in ("M", "m", "L", "l"):
            if i + 1 >= len(tokens):
                raise UnsupportedFeature(f"路径命令 {cmd} 后缺少坐标参数")
            x, y = float(tok), float(tokens[i + 1])
            i += 2
            rel = cmd.islower()
            if cursor is not None and rel:
                x, y = cursor[0] + x, cursor[1] + y
            if cmd in ("M", "m"):
                if current:
                    subpaths.append(current)
                current = [("M", [(x, y)])]
                start = (x, y)
            else:
                current.append(("L", [(x, y)]))
            cursor = (x, y)
            # 后续隐式坐标对：M 后视为 L
            cmd = "L" if cmd == "M" else ("l" if rel else "L")
        elif cmd in ("H", "h"):
            x = float(tok)
            i += 1
            y = cursor[1]
            if cmd == "h":
                x += cursor[0]
            current.append(("L", [(x, y)]))
            cursor = (x, y)
        elif cmd in ("V", "v"):
            y = float(tok)
            i += 1
            x = cursor[0]
            if cmd == "v":
                y += cursor[1]
            current.append(("L", [(x, y)]))
            cursor = (x, y)
        elif cmd in ("Z", "z"):
            i += 1
            current.append(("Z", []))
            if start is not None:
                cursor = start
        else:
            raise UnsupportedFeature(f"路径状态异常，命令={cmd!r}")
    if current:
        subpaths.append(current)
    return subpaths


def _make_point(node: RenderedNode, lx: float, ly: float, kind: str,
                segment_index: int) -> RawPoint:
    x, y = geometry.apply(node.ctm, lx, ly)
    clipped, detail = _clipped(node.clip_stages, x, y)
    return RawPoint(
        x=x, y=y, source_tag=node.tag, source_id=node.id_attr,
        transform_chain=list(node.transform_chain), kind=kind,
        clipped=clipped, clip_detail=detail, segment_index=segment_index,
    )


def extract_series(nodes: list[RenderedNode]) -> list[Series]:
    series_list = []
    for node in nodes:
        if node.hidden:
            continue
        if node.clip_unsupported:
            # 协议外裁剪：几何不可信，明确跳过而不是猜测
            series_list.append(Series(
                source_id=node.id_attr, source_tag=node.tag,
                skipped=[f"<{node.tag}> 使用了协议子集外的 clipPath "
                         "(objectBoundingBox 或非矩形)，跳过该元素"],
            ))
            continue

        if node.tag == "polyline":
            s = Series(source_id=node.id_attr, source_tag="polyline")
            prev = None
            seg = 0
            for lx, ly in node.points:
                pt = _make_point(node, lx, ly, "vertex", seg)
                if prev is not None:
                    # 画布上的大跳变视为断点（生成器缺失数据的表现形式）
                    gap = math.hypot(pt.x - prev.x, pt.y - prev.y)
                    if gap > 150:
                        seg += 1
                        s.breaks.append(
                            f"第 {seg} 段与第 {seg + 1} 段之间存在 "
                            f"{gap:.0f}px 缺口，按断点处理（不连线、不取插值）"
                        )
                        pt.segment_index = seg
                s.points.append(pt)
                prev = pt
            series_list.append(s)

        elif node.tag == "polygon":
            s = Series(source_id=node.id_attr, source_tag="polygon")
            for idx, (lx, ly) in enumerate(node.points):
                s.points.append(_make_point(node, lx, ly, "vertex", 0))
            series_list.append(s)

        elif node.tag == "path":
            s = Series(source_id=node.id_attr, source_tag="path")
            try:
                subpaths = parse_path_d(node.path_d)
            except UnsupportedFeature as exc:
                s.skipped.append(f"路径解析失败：{exc}")
                series_list.append(s)
                continue
            for seg_idx, sub in enumerate(subpaths):
                if seg_idx > 0:
                    s.breaks.append(
                        f"M 移笔开始第 {seg_idx + 1} 个独立子路径，按断点处理"
                    )
                for cmd, pts in sub:
                    if cmd in ("M", "L"):
                        for lx, ly in pts:
                            s.points.append(
                                _make_point(node, lx, ly, "vertex", seg_idx)
                            )
            series_list.append(s)

        elif node.tag in ("circle", "ellipse"):
            rx, ry = node.radius
            cx_canvas, _ = geometry.apply(node.ctm, node.center[0], 0)
            # 近似画布半径（等比场景足够；含非等比缩放时取较大值保守判定）
            p1 = geometry.apply(node.ctm, node.center[0] - rx,
                                node.center[1] - ry)
            p2 = geometry.apply(node.ctm, node.center[0] + rx,
                                node.center[1] + ry)
            r_est = max(abs(p2[0] - p1[0]), abs(p2[1] - p1[1])) / 2
            if r_est > MARKER_MAX_RADIUS:
                continue  # 装饰大圆，不是数据标记
            s = Series(source_id=node.id_attr, source_tag=node.tag)
            s.points.append(
                _make_point(node, node.center[0], node.center[1], "marker", 0)
            )
            series_list.append(s)

        elif node.tag == "rect":
            x, y, w, h = node.rect
            if max(w, h) > MARKER_MAX_SIDE:
                continue
            s = Series(source_id=node.id_attr, source_tag="rect")
            s.points.append(
                _make_point(node, x + w / 2, y + h / 2, "marker", 0)
            )
            series_list.append(s)
    return series_list


def merge_markers(series_list: list[Series]) -> list[Series]:
    """标记点优先并入折线顶点（避免同一观测点重复计数）。

    - 6px 内找到折线顶点：标记合并到该顶点，标记系列不再单独输出
    - 找不到：标记作为独立系列保留（图上可能只有标记没有连线）
    """
    polylines = [s for s in series_list
                 if s.source_tag in ("polyline", "polygon", "path")
                 and s.points]
    standalone = []
    merge_radius = DEDUP_TOL * 10
    for s in series_list:
        if s.source_tag not in ("circle", "ellipse", "rect") or not s.points:
            continue
        marker = s.points[0]
        best, best_d = None, merge_radius
        for pl in polylines:
            for pt in pl.points:
                d = math.hypot(pt.x - marker.x, pt.y - marker.y)
                if d < best_d:
                    best, best_d = pt, d
        if best is not None:
            best.kind = "vertex+marker"
            best.source_id = best.source_id or marker.source_id
        else:
            standalone.append(s)
    # 折线顶点本身去重（同画布坐标的重复顶点）
    for pl in polylines:
        unique = []
        for pt in pl.points:
            if any(math.hypot(pt.x - q.x, pt.y - q.y) < DEDUP_TOL
                   for q in unique):
                continue
            unique.append(pt)
        pl.points = unique
    # 保留带跳过原因的空系列（协议外裁剪/曲线等证据不能丢）
    skipped_series = [s for s in series_list if s.skipped]
    return polylines + standalone + skipped_series
