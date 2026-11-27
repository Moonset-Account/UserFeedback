"""取证坐标溯源模块 (forensic coordinate provenance).

一张扫描档案可能由多个原始页面拼版而成，之后又经历旋转、裁切、重新拼版。
本模块记录每一步的：

* 来源矩形 (source rectangle)
* 整数缩放或四分之一转 (integer scale / quarter-turn rotation)
* 目标位置 (target position)
* 绘制次序 (draw order)

并支持对成品中的任意矩形选区做逆向溯源，拆出实际可见部分各自来自哪一页、
哪一片原始坐标。被后画内容（含空白）覆盖的区域不会出现在结果中；同一页
重复印刷的不同出现位置会分别保留。

坐标约定
--------
所有矩形均为半开像素区间 ``[x0, y0, x1, y1)``，满足 ``x0 < x1`` 且
``y0 < y1``，宽度为 ``x1 - x0``，高度为 ``y1 - y0``。

缩放约定
--------
整数缩放因子 ``s``：

* ``s >= 2`` 为放大，源像素 (x, y) 覆盖目标的 ``s*s`` 个像素格；
* ``s <= -2`` 为缩小，``|s|`` 个源像素聚合进一个目标像素格。

缩小时，一个目标像素格可能对应多个源像素；逆向溯源会返回覆盖这些源像素
的**完整来源范围**（取下取整、上取整），而不是只倒推该格中心。
"""

from __future__ import annotations

from collections import namedtuple
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    "Rect",
    "Transform",
    "PageSource",
    "CanvasSource",
    "BlankSource",
    "Step",
    "Fragment",
    "ProvenanceModel",
    "rect_intersect",
    "rect_subtract",
    "merge_fragments",
]


# --------------------------------------------------------------------------- #
# 基础类型
# --------------------------------------------------------------------------- #


# 轻量 namedtuple，支持下标、解包与字段名访问；构造时强制非空与整数校验。
_RectTuple = namedtuple("_RectTuple", ["x0", "y0", "x1", "y1"])


def Rect(x0: int, y0: int, x1: int, y1: int) -> "_RectTuple":  # noqa: N802
    """构造半开像素矩形 ``[x0, y0, x1, y1)``，要求严格非空且坐标为整数。"""
    for axis, v in (("x0", x0), ("y0", y0), ("x1", x1), ("y1", y1)):
        if not _is_int(v):
            raise TypeError(f"Rect.{axis} 必须是整数，得到 {v!r}")
    if x0 >= x1 or y0 >= y1:
        raise ValueError(
            f"Rect 必须是非空半开矩形 (x0<x1, y0<y1)，得到 ({x0},{y0},{x1},{y1})"
        )
    return _RectTuple(x0, y0, x1, y1)


Rect.__doc__ = "半开像素矩形 [x0, y0, x1, y1) 的工厂函数。"


def _is_int(v: object) -> bool:
    # 拒绝 bool（bool 是 int 的子类），避免 True/False 被当作坐标。
    return isinstance(v, int) and not isinstance(v, bool)


def _check_rect(r: Sequence[int], name: str = "rect") -> Rect:
    if not isinstance(r, (tuple, list)) or len(r) != 4:
        raise TypeError(f"{name} 必须是长度为 4 的序列，得到 {r!r}")
    x0, y0, x1, y1 = r
    for axis, v in (("x0", x0), ("y0", y0), ("x1", x1), ("y1", y1)):
        if not _is_int(v):
            raise TypeError(f"{name}.{axis} 必须是整数，得到 {v!r}")
    if x0 >= x1 or y0 >= y1:
        raise ValueError(
            f"{name} 必须是非空半开矩形 (x0<x1, y0<y1)，得到 {tuple(r)}"
        )
    return Rect(x0, y0, x1, y1)


def rect_intersect(a: Rect, b: Rect) -> Optional[Rect]:
    """两个半开矩形的交集；不相交或交集为空时返回 ``None``。"""
    x0 = max(a.x0, b.x0)
    y0 = max(a.y0, b.y0)
    x1 = min(a.x1, b.x1)
    y1 = min(a.y1, b.y1)
    if x0 < x1 and y0 < y1:
        return Rect(x0, y0, x1, y1)
    return None


def rect_subtract(a: Rect, b: Rect) -> List[Rect]:
    """从半开矩形 ``a`` 中减去与 ``b`` 的重叠部分，返回最多 4 个不相交矩形。

    采用“先切水平条带、再切中间竖直条带”的方式，得到的分片彼此不重叠且
    并集恰好等于半开区间意义下的 ``a \\ b``；每个分片都经过非空校验。
    """
    hit = rect_intersect(a, b)
    if hit is None:
        return [a]

    parts: List[Rect] = []
    # 上、下水平条带（a 的完整宽度）
    if a.y0 < hit.y0:
        parts.append(Rect(a.x0, a.y0, a.x1, hit.y0))
    if hit.y1 < a.y1:
        parts.append(Rect(a.x0, hit.y1, a.x1, a.y1))
    # 中间条带 [hit.y0, hit.y1) 里被切走左右两块
    if a.x0 < hit.x0:
        parts.append(Rect(a.x0, hit.y0, hit.x0, hit.y1))
    if hit.x1 < a.x1:
        parts.append(Rect(hit.x1, hit.y0, a.x1, hit.y1))
    return parts


def _rects_abut(a: Rect, b: Rect) -> Optional[Rect]:
    """若两个半开矩形水平/竖直相接且另一维完全重合，返回合并矩形，否则 None。"""
    if a.y0 == b.y0 and a.y1 == b.y1 and (a.x1 == b.x0 or b.x1 == a.x0):
        return Rect(min(a.x0, b.x0), a.y0, max(a.x1, b.x1), a.y1)
    if a.x0 == b.x0 and a.x1 == b.x1 and (a.y1 == b.y0 or b.y1 == a.y0):
        return Rect(a.x0, min(a.y0, b.y0), a.x1, max(a.y1, b.y1))
    return None


def merge_fragments(fragments: Sequence[Fragment]) -> List[Fragment]:
    """合并同一来源（``page_id`` 与 ``step_path`` 相同）的相邻片段。

    覆盖切割会让一个步骤的可见区域散落在多个互不相交的矩形条带中。当两个
    分片在**目标坐标系**和**来源页坐标系**中都恰好可以拼成更大矩形（水平
    或竖直相接、另一维完全重合）时，将它们合并，避免把同一来源的连续可见
    区域无谓拆开。不同来源、不同步骤路径、或几何上不相邻的片段永不合并。
    合并保持面积守恒（半开区间下不重不漏），且合并后的 ``page_rect`` 与
    ``target_rect`` 仍满足该步骤链的几何对应关系。
    """
    pending = list(fragments)
    merged: List[Fragment] = []
    while pending:
        current = pending.pop()
        combined_any = False
        for i, other in enumerate(pending):
            if (
                other.page_id != current.page_id
                or other.step_path != current.step_path
            ):
                continue
            new_target = _rects_abut(current.target_rect, other.target_rect)
            new_page = _rects_abut(current.page_rect, other.page_rect)
            if new_target is not None and new_page is not None:
                current = Fragment(
                    page_id=current.page_id,
                    page_rect=new_page,
                    target_rect=new_target,
                    step_path=current.step_path,
                )
                pending.pop(i)
                combined_any = True
                break
        if combined_any:
            pending.append(current)
        else:
            merged.append(current)
    return merged


class Transform(Enum):
    """单步几何变换类型。"""

    IDENTITY = "identity"
    """平移拼版：不做缩放或旋转，仅按目标位置放置。"""

    SCALE = "scale"
    """整数缩放，因子由 :class:`Step` 的 ``factor`` 给出。"""

    ROTATE_90 = "rotate_90"
    """顺时针四分之一转（90°）。"""

    ROTATE_180 = "rotate_180"
    """顺时针半转（180°）。"""

    ROTATE_270 = "rotate_270"
    """顺时针四分之三转（270°，等价于逆时针 90°）。"""


@dataclass(frozen=True)
class PageSource:
    """原始扫描页（溯源叶子）。

    ``page_id`` 为调用方给定的唯一标识；同一页若被多次拼版，应使用相同
    ``page_id`` 建立多个 :class:`Step`，查询结果中会保留各自的出现位置。
    """

    page_id: str
    width: int
    height: int

    def __post_init__(self) -> None:
        if not isinstance(self.page_id, str) or not self.page_id:
            raise ValueError("page_id 必须是非空字符串")
        if not _is_int(self.width) or not _is_int(self.height):
            raise TypeError("页面宽高必须是整数")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("页面宽高必须为正数")


@dataclass(frozen=True)
class CanvasSource:
    """中间/成品画布：另一个可被继续拼版的二维表面。"""

    canvas_id: str
    width: int
    height: int

    def __post_init__(self) -> None:
        if not isinstance(self.canvas_id, str) or not self.canvas_id:
            raise ValueError("canvas_id 必须是非空字符串")
        if not _is_int(self.width) or not _is_int(self.height):
            raise TypeError("画布宽高必须是整数")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("画布宽高必须为正数")


@dataclass(frozen=True)
class BlankSource:
    """空白来源。

    空白步骤用于模拟“后画的空白覆盖”：它会按绘制次序遮盖更早的内容，但
    不会产生任何溯源片段。空白本身是一块有尺寸的矩形表面，``width`` /
    ``height`` 即被贴到目标画布上的白纸尺寸（空白步骤不允许再裁剪
    ``source_rect``）。
    """

    width: int
    height: int
    blank_id: str = "blank"

    def __post_init__(self) -> None:
        if not _is_int(self.width) or not _is_int(self.height):
            raise TypeError("空白宽高必须是整数")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("空白宽高必须为正数")
        if not isinstance(self.blank_id, str) or not self.blank_id:
            raise ValueError("blank_id 必须是非空字符串")


@dataclass(frozen=True)
class Step:
    """一次绘制操作。

    属性
    ----
    step_id:
        该步骤的唯一标识，会出现在溯源结果的 ``step_path`` 中。
    target:
        目标画布 ID（必须已通过 :meth:`ProvenanceModel.add_canvas` 注册）。
    source:
        :class:`PageSource` / :class:`CanvasSource` / :class:`BlankSource`。
    source_rect:
        在来源表面坐标系中的半开来源矩形；空白步骤可省略。
    transform:
        :class:`Transform`，默认为纯平移。
    factor:
        仅当 ``transform == Transform.SCALE`` 时使用。绝对值 >= 2；
        正数放大，负数缩小（绝对值为聚合的源像素边长）。
    target_x, target_y:
        变换后结果矩形左上角在目标画布上的像素坐标，可为负数（表示裁切
        后只有一部分落在画布内）。
    draw_order:
        绘制次序，整数；同一目标画布上数值小的先画，被数值大的覆盖。
    """

    step_id: str
    target: str
    source: object
    source_rect: Optional[Rect]
    transform: Transform = Transform.IDENTITY
    factor: Optional[int] = None
    target_x: int = 0
    target_y: int = 0
    draw_order: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.step_id, str) or not self.step_id:
            raise ValueError("step_id 必须是非空字符串")
        if not isinstance(self.target, str) or not self.target:
            raise ValueError("target 必须是非空字符串")
        if not isinstance(self.source, (PageSource, CanvasSource, BlankSource)):
            raise TypeError(
                "source 必须是 PageSource / CanvasSource / BlankSource"
            )
        if not _is_int(self.target_x) or not _is_int(self.target_y):
            raise TypeError("target_x/target_y 必须是整数")
        if not _is_int(self.draw_order):
            raise TypeError("draw_order 必须是整数")
        if not isinstance(self.transform, Transform):
            raise TypeError("transform 必须是 Transform 枚举成员")

        if isinstance(self.source, BlankSource):
            if self.source_rect is not None:
                raise ValueError("空白步骤不允许设置 source_rect")
            if self.transform is not Transform.IDENTITY:
                raise ValueError("空白步骤只支持平移（IDENTITY），不做缩放/旋转")
        else:
            if self.source_rect is None:
                raise ValueError("非空白步骤必须提供 source_rect")
            object.__setattr__(
                self, "source_rect", _check_rect(self.source_rect, "source_rect")
            )
            sr = self.source_rect
            if (
                sr.x0 < 0
                or sr.y0 < 0
                or sr.x1 > self.source.width
                or sr.y1 > self.source.height
            ):
                raise ValueError(
                    f"source_rect {tuple(sr)} 越界，来源表面尺寸为 "
                    f"{self.source.width}x{self.source.height}"
                )

        if self.transform is Transform.SCALE:
            if not _is_int(self.factor) or abs(self.factor) < 2:
                raise ValueError(
                    "SCALE 步骤的 factor 必须是绝对值 >= 2 的非零整数"
                )
        elif self.factor is not None:
            raise ValueError("只有 SCALE 步骤允许设置 factor")


@dataclass(frozen=True)
class Fragment:
    """一条可见溯源片段。

    属性
    ----
    page_id:
        最终落到的原始页 ID。
    page_rect:
        在该原始页坐标系中的半开来源矩形。缩小时这里是覆盖目标格的
        **完整源像素范围**，可能比目标格反推的中心单像素更宽。
    target_rect:
        该片段在发起查询的成品画布坐标系中的半开矩形（即选区中实际可见
        且归属于此来源的那一块）。
    step_path:
        从成品画布回溯到原始页所经过的步骤 ID，顺序为“最靠近成品的步骤
        在前，最靠近原始页的步骤在后”。
    """

    page_id: str
    page_rect: Rect
    target_rect: Rect
    step_path: Tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# 几何变换：脚印、逆向映射、正向映射
# --------------------------------------------------------------------------- #


def _footprint(step: Step) -> Tuple[int, int]:
    """计算来源矩形经过变换后的结果宽高。"""
    if isinstance(step.source, BlankSource):
        # Step 校验已保证空白步骤只能是 IDENTITY
        return step.source.width, step.source.height
    sr = step.source_rect
    w = sr.x1 - sr.x0
    h = sr.y1 - sr.y0
    if step.transform is Transform.IDENTITY:
        return w, h
    if step.transform is Transform.SCALE:
        s = step.factor
        if s > 0:
            return w * s, h * s
        k = -s  # k x k 个源像素聚合为一格
        # 只允许整除的干净聚合，避免边缘出现分数格的歧义。
        if w % k != 0 or h % k != 0:
            raise ValueError(
                f"缩小步骤 {step.step_id} 要求来源宽高可被 {k} 整除，"
                f"实际来源尺寸为 {w}x{h}"
            )
        return w // k, h // k
    if step.transform is Transform.ROTATE_180:
        # 半转不交换宽高
        return w, h
    # 90° / 270° 四分之一转交换宽高
    return h, w


def _invert_rect(step: Step, t: Rect) -> Rect:
    """把**结果坐标系**中的矩形 ``t`` 逆映射回来源坐标系。

    调用方保证 ``t`` 完全落在该步骤的结果矩形内部。
    对缩小变换返回完整来源范围（含落入同一目标格的全部源像素）。
    """
    sr = step.source_rect
    tx, ty = step.target_x, step.target_y
    # 相对结果矩形左上角的局部坐标
    lx0, ly0 = t.x0 - tx, t.y0 - ty
    lx1, ly1 = t.x1 - tx, t.y1 - ty

    tr = step.transform
    if tr is Transform.IDENTITY:
        return Rect(sr.x0 + lx0, sr.y0 + ly0, sr.x0 + lx1, sr.y0 + ly1)

    if tr is Transform.SCALE:
        s = step.factor
        if s > 0:  # 放大：一格对应源中的 1/s，结果必然与 s 对齐
            if lx0 % s or ly0 % s or lx1 % s or ly1 % s:
                raise ValueError(
                    f"放大步骤 {step.step_id} 逆映射要求边界与因子 {s} 对齐"
                )
            return Rect(
                sr.x0 + lx0 // s,
                sr.y0 + ly0 // s,
                sr.x0 + lx1 // s,
                sr.y0 + ly1 // s,
            )
        k = -s  # 缩小：k 个源像素 -> 1 格，返回完整来源范围
        return Rect(
            sr.x0 + lx0 * k,
            sr.y0 + ly0 * k,
            sr.x0 + lx1 * k,
            sr.y0 + ly1 * k,
        )

    w = sr.x1 - sr.x0
    h = sr.y1 - sr.y0

    if tr is Transform.ROTATE_90:  # 顺时针 90°
        # 正向: (sx, sy) -> (h - 1 - sy, sx)（局部像素坐标）
        # 区间: x' ∈ [h - sy1, h - sy0), y' ∈ [sx0, sx1)
        return Rect(
            sr.x0 + ly0,
            sr.y0 + (h - lx1),
            sr.x0 + ly1,
            sr.y0 + (h - lx0),
        )
    if tr is Transform.ROTATE_180:
        return Rect(
            sr.x0 + (w - lx1),
            sr.y0 + (h - ly1),
            sr.x0 + (w - lx0),
            sr.y0 + (h - ly0),
        )
    # ROTATE_270（顺时针 270° == 逆时针 90°）
    # 正向: (sx, sy) -> (sy, w - 1 - sx)
    # 区间: x' ∈ [sy0, sy1), y' ∈ [w - sx1, w - sx0)
    return Rect(
        sr.x0 + (w - ly1),
        sr.y0 + lx0,
        sr.x0 + (w - ly0),
        sr.y0 + lx1,
    )


def _project_rect(step: Step, r: Rect) -> Rect:
    """把**来源坐标系**中的矩形 ``r`` 正映射到结果坐标系（含目标偏移）。

    调用方保证 ``r`` 完全落在 ``step.source_rect`` 内。
    """
    sr = step.source_rect
    tx, ty = step.target_x, step.target_y
    # 相对 source_rect 左上角的局部来源坐标
    lx0, ly0 = r.x0 - sr.x0, r.y0 - sr.y0
    lx1, ly1 = r.x1 - sr.x0, r.y1 - sr.y0
    w = sr.x1 - sr.x0
    h = sr.y1 - sr.y0

    tr = step.transform
    if tr is Transform.IDENTITY:
        return Rect(tx + lx0, ty + ly0, tx + lx1, ty + ly1)

    if tr is Transform.SCALE:
        s = step.factor
        if s > 0:
            return Rect(tx + lx0 * s, ty + ly0 * s, tx + lx1 * s, ty + ly1 * s)
        k = -s
        # 任意来源矩形投影到目标格：floor 左界、ceil 右界，保证非空且包含
        # 所有受影响的目标格。
        return Rect(
            tx + lx0 // k,
            ty + ly0 // k,
            tx + -(-lx1 // k),
            ty + -(-ly1 // k),
        )

    if tr is Transform.ROTATE_90:
        # x' ∈ [h - ly1, h - ly0), y' ∈ [lx0, lx1)
        return Rect(tx + h - ly1, ty + lx0, tx + h - ly0, ty + lx1)
    if tr is Transform.ROTATE_180:
        return Rect(
            tx + w - lx1, ty + h - ly1, tx + w - lx0, ty + h - ly0
        )
    # ROTATE_270: x' ∈ [ly0, ly1), y' ∈ [w - lx1, w - lx0)
    return Rect(tx + ly0, ty + w - lx1, tx + ly1, ty + w - lx0)


# --------------------------------------------------------------------------- #
# 溯源模型
# --------------------------------------------------------------------------- #


class ProvenanceModel:
    """记录全部表面（页面/画布）与绘制步骤，并提供逆向溯源查询。

    典型用法::

        model = ProvenanceModel()
        model.add_page(PageSource("p1", 100, 200))
        model.add_canvas(CanvasSource("final", 500, 500))
        model.add_step(Step("s1", "final",
                            model.pages["p1"], Rect(0, 0, 100, 200),
                            target_x=10, target_y=20, draw_order=0))
        fragments = model.query("final", Rect(0, 0, 500, 500))
    """

    def __init__(self) -> None:
        self.pages: Dict[str, PageSource] = {}
        self.canvases: Dict[str, CanvasSource] = {}
        # target canvas_id -> 该画布上的步骤（按 draw_order 升序）
        self._steps: Dict[str, List[Step]] = {}
        self._step_ids: Dict[str, Step] = {}
        self._footprints: Dict[str, Rect] = {}

    # -- 注册 -------------------------------------------------------------- #

    def add_page(self, page: PageSource) -> None:
        if not isinstance(page, PageSource):
            raise TypeError("page 必须是 PageSource")
        if page.page_id in self.pages or page.page_id in self.canvases:
            raise ValueError(f"ID {page.page_id!r} 已被占用")
        self.pages[page.page_id] = page

    def add_canvas(self, canvas: CanvasSource) -> None:
        if not isinstance(canvas, CanvasSource):
            raise TypeError("canvas 必须是 CanvasSource")
        if canvas.canvas_id in self.pages or canvas.canvas_id in self.canvases:
            raise ValueError(f"ID {canvas.canvas_id!r} 已被占用")
        self.canvases[canvas.canvas_id] = canvas
        self._steps[canvas.canvas_id] = []

    def add_step(self, step: Step) -> None:
        """登记一个绘制步骤并做全部结构性校验。"""
        if not isinstance(step, Step):
            raise TypeError("step 必须是 Step 实例")
        if step.step_id in self._step_ids:
            raise ValueError(f"step_id {step.step_id!r} 已存在")
        if step.target not in self.canvases:
            raise ValueError(
                f"目标画布 {step.target!r} 未注册（步骤 {step.step_id}）"
            )

        src = step.source
        if isinstance(src, PageSource):
            if src.page_id not in self.pages:
                raise ValueError(
                    f"来源页 {src.page_id!r} 未注册（步骤 {step.step_id}）"
                )
            if self.pages[src.page_id] is not src:
                raise ValueError(
                    f"步骤 {step.step_id} 的 PageSource 与注册实例不一致"
                )
        elif isinstance(src, CanvasSource):
            if src.canvas_id not in self.canvases:
                raise ValueError(
                    f"来源画布 {src.canvas_id!r} 未注册（步骤 {step.step_id}）"
                )
            if self.canvases[src.canvas_id] is not src:
                raise ValueError(
                    f"步骤 {step.step_id} 的 CanvasSource 与注册实例不一致"
                )
            self._check_no_cycle(step)

        target_canvas = self.canvases[step.target]
        fw, fh = _footprint(step)
        footprint = Rect(
            step.target_x, step.target_y, step.target_x + fw, step.target_y + fh
        )
        # 结果矩形必须与目标画布有交集；完全落在画布外没有取证意义。
        if (
            footprint.x1 <= 0
            or footprint.y1 <= 0
            or footprint.x0 >= target_canvas.width
            or footprint.y0 >= target_canvas.height
        ):
            raise ValueError(
                f"步骤 {step.step_id} 的结果矩形 {tuple(footprint)} "
                f"完全落在目标画布 {step.target!r} 之外"
            )

        bucket = self._steps[step.target]
        if any(s.draw_order == step.draw_order for s in bucket):
            raise ValueError(
                f"画布 {step.target!r} 上已存在 draw_order={step.draw_order} "
                "的步骤；绘制次序必须构成全序"
            )

        self._step_ids[step.step_id] = step
        self._footprints[step.step_id] = footprint
        bucket.append(step)
        bucket.sort(key=lambda s: s.draw_order)

    def _check_no_cycle(self, new_step: Step) -> None:
        """若 new_step 生效后画布引用图出现环，则拒绝。"""
        # 从被引用的来源画布出发 DFS，若能回到 new_step.target 则成环。
        stack = [new_step.source.canvas_id]
        seen = set()
        while stack:
            cur = stack.pop()
            if cur == new_step.target:
                raise ValueError(
                    f"步骤 {new_step.step_id} 会在画布引用图中形成环"
                )
            if cur in seen:
                continue
            seen.add(cur)
            for s in self._steps.get(cur, ()):
                if isinstance(s.source, CanvasSource):
                    stack.append(s.source.canvas_id)

    # -- 查询 -------------------------------------------------------------- #

    def steps_on(self, canvas_id: str) -> Tuple[Step, ...]:
        """返回某画布上按绘制次序升序排列的全部步骤。"""
        if canvas_id not in self.canvases:
            raise ValueError(f"画布 {canvas_id!r} 未注册")
        return tuple(self._steps[canvas_id])

    def footprint_of(self, step_id: str) -> Rect:
        """返回某步骤在其目标画布上的结果矩形（含可能被裁切的越界部分）。"""
        if step_id not in self._footprints:
            raise ValueError(f"步骤 {step_id!r} 不存在")
        return self._footprints[step_id]

    def query(self, canvas_id: str, selection: Sequence[int]) -> List[Fragment]:
        """对成品画布 ``canvas_id`` 上的半开选区做逆向溯源。

        参数
        ----
        canvas_id:
            发起查询的画布 ID（通常是最终成品画布）。
        selection:
            ``[x0, y0, x1, y1)`` 形式的半开矩形；允许部分或完全越界，
            越界部分会被安静裁掉。

        返回
        ----
        list[Fragment]
            可见片段列表，按
            ``(target_rect 自上而下、自左而右, page_id, step_path)``
            排序。被后画内容（含空白）覆盖的区域不会出现；选区与任何
            内容都不相交时返回空列表。
        """
        if canvas_id not in self.canvases:
            raise ValueError(f"画布 {canvas_id!r} 未注册")
        sel = _check_rect(selection, "selection")
        canvas = self.canvases[canvas_id]
        clipped = rect_intersect(
            sel, Rect(0, 0, canvas.width, canvas.height)
        )
        if clipped is None:
            return []
        fragments = self._collect(canvas_id, clipped, ())
        fragments.sort(
            key=lambda f: (
                f.target_rect.y0,
                f.target_rect.x0,
                f.target_rect.y1,
                f.target_rect.x1,
                f.page_id,
                f.step_path,
            )
        )
        return fragments

    # -- 递归实现 ---------------------------------------------------------- #

    def _collect(
        self, canvas_id: str, region: Rect, path: Tuple[str, ...]
    ) -> List[Fragment]:
        """收集画布 ``canvas_id`` 上区域 ``region`` 内的可见叶子片段。

        返回片段的 ``target_rect`` 均以 **region 所在画布的坐标系**表示。
        """
        canvas = self.canvases[canvas_id]
        region = rect_intersect(region, Rect(0, 0, canvas.width, canvas.height))
        if region is None:
            return []

        # 剩余未被覆盖的区域，始终保持为互不相交的半开矩形列表。
        uncovered: List[Rect] = [region]
        results: List[Fragment] = []

        # 自顶向下：最上层（draw_order 最大）先认领它盖住的部分。
        for step in reversed(self._steps[canvas_id]):
            if not uncovered:
                break
            footprint = self._footprints[step.step_id]
            new_uncovered: List[Rect] = []
            for piece in uncovered:
                hit = rect_intersect(piece, footprint)
                if hit is None:
                    new_uncovered.append(piece)
                    continue
                # piece 中未被本步骤盖住的部分继续留给更早的步骤。
                new_uncovered.extend(rect_subtract(piece, hit))

                if isinstance(step.source, BlankSource):
                    continue  # 空白覆盖：不产生溯源片段

                source_rect = _invert_rect(step, hit)
                # 路径从成品向原始页延伸：越靠近成品的步骤越靠前。
                next_path = path + (step.step_id,)

                if isinstance(step.source, PageSource):
                    results.append(
                        Fragment(
                            page_id=step.source.page_id,
                            page_rect=source_rect,
                            target_rect=hit,
                            step_path=next_path,
                        )
                    )
                else:  # CanvasSource -> 递归进入来源画布
                    sub = self._collect(
                        step.source.canvas_id, source_rect, next_path
                    )
                    # 先在来源画布坐标系内合并相邻同源分片，再投影到当前
                    # 画布坐标系，保证合并后的 page/target 矩形仍严格对应。
                    for frag in merge_fragments(sub):
                        projected = _project_rect(step, frag.target_rect)
                        results.append(
                            Fragment(
                                page_id=frag.page_id,
                                page_rect=frag.page_rect,
                                target_rect=projected,
                                step_path=frag.step_path,
                            )
                        )
            uncovered = new_uncovered

        # 同一画布上被切割开的同源相邻分片同样合并后再返回给上层。
        return merge_fragments(results)
