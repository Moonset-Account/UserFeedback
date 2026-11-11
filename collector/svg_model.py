"""SVG 文档解析与渲染树建模（仅使用公开可见图形元素）。

输出的 RenderedNode 已带：
- 画布坐标（嵌套 transform、viewBox 已展开为 CTM）
- 可见性判定（display:none / visibility:hidden / opacity:0）
- 生效中的裁剪矩形（协议子集：userSpaceOnUse 下的矩形 clipPath）

不读取任何注释、metadata、私有命名空间或 data-* 属性中的数据。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from . import geometry
from .transforms import UnsupportedFeature, parse_transform, combine

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
NS = {"svg": SVG_NS, "xlink": XLINK_NS}

_VIEWBOX_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_UNIT_RE = re.compile(r"^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?(px)?\s*$")


def _q(tag: str) -> str:
    return f"{{{SVG_NS}}}{tag}"


def parse_number(value: str | None, default: float | None = None) -> float:
    """解析 SVG 长度属性。子集：纯数字与 px；em/%/pt 等不支持。"""
    if value is None:
        if default is not None:
            return default
        raise UnsupportedFeature("缺少必要的数值属性")
    text = value.strip()
    if _UNIT_RE.match(text):
        return float(text.rstrip("px").strip() or 0)
    raise UnsupportedFeature(f"不支持的长度单位或数值: {value!r}")


@dataclass
class RenderedNode:
    tag: str                 # 不含命名空间的本地标签名
    attrs: dict              # 原始属性（本地名）
    ctm: tuple               # 该元素自身坐标系 -> 画布坐标
    transform_chain: list    # 每层 transform 字符串（含元素自身），用于溯源
    clip_stages: list        # 嵌套裁剪阶段；每阶段是画布坐标矩形 list，
    #                        # 阶段内取并集、阶段间取交集
    clip_unsupported: bool   # 存在协议外裁剪，节点几何不可信
    hidden: bool
    hidden_reason: str | None
    # 几何（元素自身坐标系下的原始数据，几何换算时再走 ctm）
    points: list = field(default_factory=list)        # polyline/line
    path_d: str | None = None
    center: tuple | None = None                       # circle/ellipse
    radius: tuple | None = None
    rect: tuple | None = None                         # (x,y,w,h)
    text: str | None = None
    text_origin: tuple | None = None

    @property
    def id_attr(self) -> str | None:
        return self.attrs.get("id")


class SvgDocument:
    def __init__(self, source: str | bytes):
        if isinstance(source, bytes):
            source = source.decode("utf-8")
        self.raw = source
        self.root = ET.fromstring(source)
        if self.root.tag != _q("svg"):
            raise UnsupportedFeature("根元素不是 <svg>")
        self._id_map: dict[str, ET.Element] = {}
        for el in self.root.iter():
            el_id = el.get("id")
            if el_id and el_id not in self._id_map:
                self._id_map[el_id] = el
        self.root_matrix = self._viewbox_matrix()
        self.warnings: list[str] = []
        self.nodes: list[RenderedNode] = []

    # ---------- viewBox ----------
    def _viewbox_matrix(self) -> tuple:
        width = parse_number(self.root.get("width"), default=300.0)
        height = parse_number(self.root.get("height"), default=150.0)
        vb = self.root.get("viewBox")
        if not vb:
            return geometry.IDENTITY
        nums = [float(n) for n in _VIEWBOX_RE.findall(vb)]
        if len(nums) != 4:
            raise UnsupportedFeature(f"viewBox 非法: {vb!r}")
        min_x, min_y, vb_w, vb_h = nums
        if vb_w <= 0 or vb_h <= 0:
            raise UnsupportedFeature("viewBox 宽高必须为正数")
        par = (self.root.get("preserveAspectRatio") or "xMidYMid meet").strip()
        if par.split()[0] not in ("none",) and "meet" not in par \
                and "slice" not in par:
            # 默认 meet
            par = par + " meet"
        if par.startswith("none"):
            scale_x, scale_y = width / vb_w, height / vb_h
            tx, ty = -min_x * scale_x, -min_y * scale_y
            return (scale_x, 0.0, 0.0, scale_y, tx, ty)
        scale = min(width / vb_w, height / vb_h)
        x_align, y_align = "xMid", "YMid"
        for token in par.split():
            if token.startswith("x"):
                x_align = token
            elif token.startswith("Y"):
                y_align = token
        extra_x = width - vb_w * scale
        extra_y = height - vb_h * scale
        tx_map = {"xMin": 0.0, "xMid": extra_x / 2, "xMax": extra_x}
        ty_map = {"YMin": 0.0, "YMid": extra_y / 2, "YMax": extra_y}
        tx = tx_map.get(x_align, extra_x / 2) - min_x * scale
        ty = ty_map.get(y_align, extra_y / 2) - min_y * scale
        return (scale, 0.0, 0.0, scale, tx, ty)

    # ---------- 可见性 ----------
    @staticmethod
    def _visibility(el: ET.Element) -> tuple[bool, str | None]:
        style = el.get("style", "")
        style_map = {}
        for decl in style.split(";"):
            if ":" in decl:
                k, v = decl.split(":", 1)
                style_map[k.strip()] = v.strip()
        display = el.get("display", style_map.get("display", "inline"))
        if display == "none":
            return True, "display:none"
        visibility = el.get("visibility", style_map.get("visibility", "visible"))
        if visibility == "hidden":
            return True, "visibility:hidden"
        if visibility == "collapse":
            return True, "visibility:collapse"
        opacity = el.get("opacity", style_map.get("opacity"))
        if opacity is not None:
            try:
                if float(opacity) == 0.0:
                    return True, "opacity:0"
            except ValueError:
                pass
        return False, None

    # ---------- 裁剪 ----------
    def _resolve_clip(self, clip_attr: str | None, ctm: tuple):
        """返回 (stages, unsupported)。

        stages 为嵌套阶段列表；每阶段是画布坐标下的矩形列表（阶段内并集）。
        协议子集：userSpaceOnUse、仅 rect 形状；其余标记 unsupported。
        """
        if not clip_attr:
            return [], False
        m = re.match(r"url\(#([^)]+)\)\s*$", clip_attr.strip())
        if not m:
            return [], True
        clip_el = self._id_map.get(m.group(1))
        if clip_el is None or clip_el.tag != _q("clipPath"):
            return [], True
        units = clip_el.get("clipPathUnits", "userSpaceOnUse")
        if units == "objectBoundingBox":
            return [], True  # 子集外：对象包围盒单位
        rects, unsupported = [], False
        clip_ctm = geometry.multiply(
            ctm, combine(parse_transform(clip_el.get("transform")))
        )
        for child in clip_el:
            if child.tag == _q("rect"):
                try:
                    x = parse_number(child.get("x"), 0.0)
                    y = parse_number(child.get("y"), 0.0)
                    w = parse_number(child.get("width"))
                    h = parse_number(child.get("height"))
                    local = combine(parse_transform(child.get("transform")))
                    m2 = geometry.multiply(clip_ctm, local)
                    pts = [geometry.apply(m2, x, y),
                           geometry.apply(m2, x + w, y),
                           geometry.apply(m2, x + w, y + h),
                           geometry.apply(m2, x, y + h)]
                    xs = [p[0] for p in pts]
                    ys = [p[1] for p in pts]
                    rects.append(geometry.Rect(
                        min(xs), min(ys), max(xs) - min(xs),
                        max(ys) - min(ys)
                    ))
                except UnsupportedFeature:
                    unsupported = True
            else:
                # circle/polygon/path/use 等裁剪形状不在本协议子集内
                unsupported = True
        stages = [rects] if rects else []
        # clipPath 自身再套 clipPath：构成新的嵌套阶段（交集）
        parent_clip = clip_el.get("clip-path")
        if parent_clip:
            more, up2 = self._resolve_clip(parent_clip, ctm)
            stages = stages + more
            unsupported = unsupported or up2
        return stages, unsupported

    # ---------- 文本 ----------
    @staticmethod
    def _text_content(el: ET.Element) -> str:
        parts = []
        if el.text:
            parts.append(el.text)
        for child in el:
            if child.tag in (_q("tspan"), _q("tref")):
                parts.append("".join(child.itertext()))
            if child.tail:
                parts.append(child.tail)
        return "".join(parts).strip()

    # ---------- 遍历 ----------
    def build(self) -> list[RenderedNode]:
        self.nodes = []
        self._walk(self.root, self.root_matrix, [], [], False, set(), False)
        return self.nodes

    def _walk(self, el: ET.Element, parent_ctm, chain, clip_stages,
              clip_unsupported, use_stack, parent_hidden):
        own_hidden, reason = self._visibility(el)
        hidden = parent_hidden or own_hidden
        if parent_hidden and reason is None:
            reason = "祖先元素不可见（display/visibility/opacity 继承）"
        own_mats = parse_transform(el.get("transform"))
        ctm = parent_ctm
        new_chain = chain
        if own_mats:
            ctm = geometry.multiply(ctm, combine(own_mats))
            new_chain = chain + [el.get("transform")]

        stages = [list(s) for s in clip_stages]
        unsupported = clip_unsupported
        if el.get("clip-path"):
            more, up = self._resolve_clip(el.get("clip-path"), ctm)
            stages = stages + more
            unsupported = unsupported or up

        tag = el.tag.split("}", 1)[-1] if "}" in el.tag else el.tag

        # <use> 展开（防循环）
        if tag == "use":
            ref = el.get("href") or el.get(f"{{{XLINK_NS}}}href")
            if ref and ref.startswith("#"):
                target = self._id_map.get(ref[1:])
                if target is not None:
                    if ref[1:] in use_stack:
                        self.warnings.append(f"<use> 引用循环 {ref}，已跳过")
                        return
                    use_ctm = geometry.multiply(
                        ctm,
                        geometry.translation(
                            parse_number(el.get("x"), 0.0),
                            parse_number(el.get("y"), 0.0),
                        ),
                    )
                    self._walk(target, use_ctm, new_chain + [f"use {ref}"],
                               stages, unsupported, use_stack | {ref[1:]},
                               hidden)
                    return
            self.warnings.append("无法解析的 <use> 引用，已跳过")
            return

        if tag == "defs":
            return  # 定义不直接渲染，仅通过引用参与

        node = RenderedNode(
            tag=tag, attrs=dict(el.attrib), ctm=ctm,
            transform_chain=new_chain, clip_stages=stages,
            clip_unsupported=unsupported, hidden=hidden, hidden_reason=reason,
        )
        known_geometry = False
        try:
            if tag == "line":
                node.points = [
                    (parse_number(el.get("x1")), parse_number(el.get("y1"))),
                    (parse_number(el.get("x2")), parse_number(el.get("y2"))),
                ]
                known_geometry = True
            elif tag in ("polyline", "polygon"):
                nums = [float(n) for n in re.findall(
                    r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?",
                    el.get("points", ""),
                )]
                node.points = list(zip(nums[0::2], nums[1::2]))
                known_geometry = True
            elif tag == "path":
                node.path_d = el.get("d", "")
                known_geometry = True
            elif tag == "circle":
                node.center = (parse_number(el.get("cx"), 0.0),
                               parse_number(el.get("cy"), 0.0))
                node.radius = (parse_number(el.get("r"), 0.0),
                               parse_number(el.get("r"), 0.0))
                known_geometry = True
            elif tag == "ellipse":
                node.center = (parse_number(el.get("cx"), 0.0),
                               parse_number(el.get("cy"), 0.0))
                node.radius = (parse_number(el.get("rx"), 0.0),
                               parse_number(el.get("ry"), 0.0))
                known_geometry = True
            elif tag == "rect":
                node.rect = (
                    parse_number(el.get("x"), 0.0),
                    parse_number(el.get("y"), 0.0),
                    parse_number(el.get("width")),
                    parse_number(el.get("height")),
                )
                known_geometry = True
            elif tag == "text":
                node.text = self._text_content(el)
                node.text_origin = (
                    parse_number(el.get("x"), 0.0),
                    parse_number(el.get("y"), 0.0),
                )
                known_geometry = True
        except UnsupportedFeature as exc:
            self.warnings.append(f"<{tag}> 几何解析失败：{exc}")

        if known_geometry:
            self.nodes.append(node)

        # 标题/描述等不携带几何，继续遍历子节点
        for child in el:
            self._walk(child, ctm, new_chain, stages, unsupported,
                       use_stack, hidden)
