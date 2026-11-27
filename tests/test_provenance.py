"""provenance 模块的单元测试。

运行：
    python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from provenance import (  # noqa: E402
    BlankSource,
    CanvasSource,
    Fragment,
    PageSource,
    ProvenanceModel,
    Rect,
    Step,
    Transform,
    rect_intersect,
    rect_subtract,
)
from provenance import _check_rect  # noqa: E402


# --------------------------------------------------------------------------- #
# 矩形代数
# --------------------------------------------------------------------------- #


class RectAlgebraTests(unittest.TestCase):
    def test_intersect_half_open_touching_is_empty(self):
        # 半开区间：仅边界相接不算相交
        self.assertIsNone(
            rect_intersect(Rect(0, 0, 10, 10), Rect(10, 0, 20, 10))
        )
        self.assertIsNone(
            rect_intersect(Rect(0, 0, 10, 10), Rect(0, 10, 10, 20))
        )

    def test_intersect_overlap(self):
        self.assertEqual(
            rect_intersect(Rect(0, 0, 10, 10), Rect(5, 6, 15, 16)),
            Rect(5, 6, 10, 10),
        )

    def test_subtract_partition_is_disjoint_and_exhaustive(self):
        a = Rect(2, 3, 22, 19)
        b = Rect(10, 7, 15, 12)
        parts = rect_subtract(a, b)
        # 分片两两不相交
        for i, p in enumerate(parts):
            for q in parts[i + 1:]:
                self.assertIsNone(rect_intersect(p, q))
        # 分片面积之和 == 原面积 - 交集面积
        area = lambda r: (r.x1 - r.x0) * (r.y1 - r.y0)
        hit = rect_intersect(a, b)
        self.assertEqual(
            sum(area(p) for p in parts), area(a) - area(hit)
        )

    def test_subtract_no_overlap_returns_original(self):
        a = Rect(0, 0, 10, 10)
        self.assertEqual(rect_subtract(a, Rect(20, 20, 30, 30)), [a])

    def test_subtract_partial_overlaps_around_edges(self):
        # b 从左侧切入
        parts = rect_subtract(Rect(0, 0, 10, 10), Rect(-5, 2, 5, 8))
        self.assertEqual(
            sorted(parts),
            sorted(
                [
                    Rect(5, 2, 10, 8),
                    Rect(0, 0, 10, 2),
                    Rect(0, 8, 10, 10),
                ]
            ),
        )
        # b 完全包含 a -> 空
        self.assertEqual(rect_subtract(Rect(0, 0, 10, 10), Rect(-1, -1, 11, 11)), [])

    def test_invalid_rect_rejected(self):
        with self.assertRaises(ValueError):
            Rect(0, 0, 0, 5)  # 空宽（x0 == x1）
        with self.assertRaises(ValueError):
            Rect(0, 5, 5, 5)  # 空高（y0 == y1）
        with self.assertRaises(TypeError):
            Rect(0, 0, True, 5)  # bool 不被接受为坐标
        with self.assertRaises(TypeError):
            _check_rect((0, 0, 5.0, 5))  # 浮点坐标


# --------------------------------------------------------------------------- #
# 正常拼版与选区
# --------------------------------------------------------------------------- #


class BasicPlacementTests(unittest.TestCase):
    def setUp(self):
        self.model = ProvenanceModel()
        self.model.add_page(PageSource("p1", 100, 200))
        self.model.add_page(PageSource("p2", 60, 80))
        self.model.add_canvas(CanvasSource("final", 300, 300))
        self.model.add_step(
            Step(
                "s1", "final", self.model.pages["p1"],
                Rect(0, 0, 100, 200),
                target_x=10, target_y=20, draw_order=0,
            )
        )
        self.model.add_step(
            Step(
                "s2", "final", self.model.pages["p2"],
                Rect(10, 20, 60, 80),
                target_x=150, target_y=40, draw_order=1,
            )
        )

    def test_full_selection_returns_both_pages(self):
        frags = self.model.query("final", Rect(0, 0, 300, 300))
        self.assertEqual(len(frags), 2)
        by_page = {f.page_id: f for f in frags}
        self.assertEqual(
            by_page["p1"].page_rect, Rect(0, 0, 100, 200)
        )
        self.assertEqual(
            by_page["p1"].target_rect, Rect(10, 20, 110, 220)
        )
        self.assertEqual(by_page["p1"].step_path, ("s1",))
        self.assertEqual(
            by_page["p2"].page_rect, Rect(10, 20, 60, 80)
        )
        self.assertEqual(
            by_page["p2"].target_rect, Rect(150, 40, 200, 100)
        )
        self.assertEqual(by_page["p2"].step_path, ("s2",))

    def test_partial_selection_clips_and_shifts_source(self):
        # 只取 p1 脚印中 [20,30)-(40,60) 的一块
        frags = self.model.query("final", Rect(20, 30, 40, 60))
        self.assertEqual(len(frags), 1)
        f = frags[0]
        self.assertEqual(f.page_id, "p1")
        self.assertEqual(f.target_rect, Rect(20, 30, 40, 60))
        # 目标偏移 (10,20) 反推回来源
        self.assertEqual(f.page_rect, Rect(10, 10, 30, 40))

    def test_selection_on_empty_area_returns_nothing(self):
        self.assertEqual(
            self.model.query("final", Rect(250, 250, 300, 300)), []
        )

    def test_out_of_bounds_selection_is_clipped(self):
        frags = self.model.query("final", Rect(-50, -50, 15, 25))
        self.assertEqual(len(frags), 1)
        # 与画布及脚印两次裁切后只剩 [10,20)-[15,25)
        self.assertEqual(frags[0].target_rect, Rect(10, 20, 15, 25))
        self.assertEqual(frags[0].page_rect, Rect(0, 0, 5, 5))

    def test_step_partly_outside_canvas_is_clipped(self):
        model = ProvenanceModel()
        model.add_page(PageSource("p", 100, 100))
        model.add_canvas(CanvasSource("c", 50, 50))
        # 脚印 [-10,-20)-[90,80)，只有左上角 60x70 落画布内，再被画布裁成 50x50
        model.add_step(
            Step("s", "c", model.pages["p"], Rect(0, 0, 100, 100),
                 target_x=-10, target_y=-20, draw_order=0)
        )
        frags = model.query("c", Rect(0, 0, 50, 50))
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0].target_rect, Rect(0, 0, 50, 50))
        self.assertEqual(frags[0].page_rect, Rect(10, 20, 60, 70))


# --------------------------------------------------------------------------- #
# 绘制次序、覆盖与空白
# --------------------------------------------------------------------------- #


class DrawOrderTests(unittest.TestCase):
    def setUp(self):
        self.model = ProvenanceModel()
        self.model.add_page(PageSource("p1", 100, 100))
        self.model.add_page(PageSource("p2", 100, 100))
        self.model.add_canvas(CanvasSource("c", 100, 100))
        # 底层整页
        self.model.add_step(
            Step("base", "c", self.model.pages["p1"],
                 Rect(0, 0, 100, 100), draw_order=0)
        )
        # 上层只盖中间一块
        self.model.add_step(
            Step("over", "c", self.model.pages["p2"],
                 Rect(0, 0, 40, 30),
                 target_x=30, target_y=40, draw_order=1)
        )

    def test_occlusion_splits_visible_regions(self):
        frags = self.model.query("c", Rect(0, 0, 100, 100))
        # 底层被切成 4 个可见分片 + 上层 1 片
        self.assertEqual(len(frags), 5)
        over = [f for f in frags if f.page_id == "p2"]
        self.assertEqual(len(over), 1)
        self.assertEqual(over[0].target_rect, Rect(30, 40, 70, 70))
        base = [f for f in frags if f.page_id == "p1"]
        self.assertEqual(len(base), 4)
        # 可见分片两两不相交，且并集覆盖整个画布
        for i, a in enumerate(frags):
            for b in frags[i + 1:]:
                self.assertIsNone(rect_intersect(a.target_rect, b.target_rect))
        area = lambda r: (r.x1 - r.x0) * (r.y1 - r.y0)
        self.assertEqual(sum(area(f.target_rect) for f in frags), 100 * 100)

    def test_query_inside_occluder_returns_only_top(self):
        frags = self.model.query("c", Rect(35, 45, 60, 65))
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0].page_id, "p2")
        self.assertEqual(frags[0].page_rect, Rect(5, 5, 30, 25))
        self.assertEqual(frags[0].step_path, ("over",))

    def test_query_straddling_occluder_boundary(self):
        # 选区跨过覆盖块左边界：一部分 p2，一部分 p1
        frags = self.model.query("c", Rect(20, 45, 40, 55))
        pages = sorted(f.page_id for f in frags)
        self.assertEqual(pages, ["p1", "p2"])
        p2 = next(f for f in frags if f.page_id == "p2")
        p1 = next(f for f in frags if f.page_id == "p1")
        self.assertEqual(p2.target_rect, Rect(30, 45, 40, 55))
        self.assertEqual(p1.target_rect, Rect(20, 45, 30, 55))
        self.assertEqual(p2.page_rect, Rect(0, 5, 10, 15))
        self.assertEqual(p1.page_rect, Rect(20, 45, 30, 55))

    def test_blank_step_covers_without_fragment(self):
        model = ProvenanceModel()
        model.add_page(PageSource("p", 100, 100))
        model.add_canvas(CanvasSource("c", 100, 100))
        model.add_step(
            Step("ink", "c", model.pages["p"],
                 Rect(0, 0, 100, 100), draw_order=0)
        )
        # 后画一张 40x30 的白纸，盖住 [25,25)-[65,55)
        model.add_step(
            Step("erase", "c", BlankSource(40, 30, "white"),
                 source_rect=None,
                 target_x=25, target_y=25, draw_order=1)
        )
        # 白纸区域内查不到任何来源片段
        self.assertEqual(model.query("c", Rect(30, 30, 60, 50)), [])
        # 白纸外仍能查到原页
        outside = model.query("c", Rect(0, 0, 25, 100))
        self.assertEqual(len(outside), 1)
        self.assertEqual(outside[0].page_id, "p")
        # 跨边界选区：只有白纸外的部分产生片段
        straddle = model.query("c", Rect(20, 30, 30, 40))
        self.assertEqual(len(straddle), 1)
        self.assertEqual(straddle[0].target_rect, Rect(20, 30, 25, 40))
        # 全画布面积守恒：可见部分恰好为 10000 - 40*30
        area = lambda r: (r.x1 - r.x0) * (r.y1 - r.y0)
        all_frags = model.query("c", Rect(0, 0, 100, 100))
        self.assertEqual(
            sum(area(f.target_rect) for f in all_frags), 10000 - 40 * 30
        )


class BlankValidationTests(unittest.TestCase):
    def test_blank_needs_positive_size(self):
        with self.assertRaises(ValueError):
            BlankSource(0, 5)
        with self.assertRaises(TypeError):
            BlankSource(True, 5)

    def test_blank_step_rejects_source_rect_and_transform(self):
        model = ProvenanceModel()
        model.add_canvas(CanvasSource("c", 10, 10))
        with self.assertRaises(ValueError):
            Step("s1", "c", BlankSource(5, 5), Rect(0, 0, 2, 2))
        with self.assertRaises(ValueError):
            Step(
                "s2", "c", BlankSource(5, 5), None,
                transform=Transform.ROTATE_90,
            )

    def test_blank_partly_off_canvas_is_clipped(self):
        model = ProvenanceModel()
        model.add_page(PageSource("p", 10, 10))
        model.add_canvas(CanvasSource("c", 10, 10))
        model.add_step(
            Step("ink", "c", model.pages["p"],
                 Rect(0, 0, 10, 10), draw_order=0)
        )
        # 白纸脚印 [-5,5)-[5,15)，与画布交集为 [0,5)-[5,10)，面积 25
        model.add_step(
            Step("erase", "c", BlankSource(10, 10), None,
                 target_x=-5, target_y=5, draw_order=1)
        )
        self.assertEqual(model.query("c", Rect(0, 5, 5, 10)), [])
        frags = model.query("c", Rect(0, 0, 10, 10))
        area = lambda r: (r.x1 - r.x0) * (r.y1 - r.y0)
        self.assertEqual(sum(area(f.target_rect) for f in frags), 100 - 25)

    def test_blank_inside_intermediate_blocks_leaf(self):
        model = ProvenanceModel()
        model.add_page(PageSource("p", 10, 10))
        model.add_canvas(CanvasSource("mid", 10, 10))
        model.add_canvas(CanvasSource("final", 10, 10))
        model.add_step(
            Step("ink", "mid", model.pages["p"],
                 Rect(0, 0, 10, 10), draw_order=0)
        )
        model.add_step(
            Step("erase", "mid", BlankSource(4, 4), None,
                 target_x=3, target_y=3, draw_order=1)
        )
        model.add_step(
            Step("place", "final", model.canvases["mid"],
                 Rect(0, 0, 10, 10), draw_order=0)
        )
        # 透过 final 看 mid 里被白纸盖掉的区域：无片段
        self.assertEqual(model.query("final", Rect(4, 4, 5, 5)), [])
        # 白纸外仍可溯源到 p
        frags = model.query("final", Rect(0, 0, 3, 3))
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0].page_id, "p")
        self.assertEqual(frags[0].step_path, ("place", "ink"))


# --------------------------------------------------------------------------- #
# 重复印刷
# --------------------------------------------------------------------------- #


class DuplicatePageTests(unittest.TestCase):
    def test_same_page_twice_keeps_beth_occurrences(self):
        model = ProvenanceModel()
        model.add_page(PageSource("p", 40, 20))
        model.add_canvas(CanvasSource("c", 200, 100))
        model.add_step(
            Step("copy_a", "c", model.pages["p"], Rect(0, 0, 40, 20),
                 target_x=0, target_y=0, draw_order=0)
        )
        model.add_step(
            Step("copy_b", "c", model.pages["p"], Rect(0, 0, 40, 20),
                 target_x=80, target_y=50, draw_order=1)
        )
        frags = model.query("c", Rect(0, 0, 200, 100))
        self.assertEqual(len(frags), 2)
        self.assertTrue(all(f.page_id == "p" for f in frags))
        positions = sorted(f.target_rect for f in frags)
        self.assertEqual(
            positions,
            [Rect(0, 0, 40, 20), Rect(80, 50, 120, 70)],
        )
        paths = sorted(f.step_path for f in frags)
        self.assertEqual(paths, [("copy_a",), ("copy_b",)])


# --------------------------------------------------------------------------- #
# 缩放：放大、缩小（完整来源范围）
# --------------------------------------------------------------------------- #


class ScaleTests(unittest.TestCase):
    def test_downscale_returns_full_source_range(self):
        model = ProvenanceModel()
        model.add_page(PageSource("p", 8, 8))
        # 8x8 按 factor=-4 缩小后脚印为 2x2，画布必须放得下
        model.add_canvas(CanvasSource("c", 4, 4))
        model.add_step(
            Step("down", "c", model.pages["p"], Rect(0, 0, 8, 8),
                 transform=Transform.SCALE, factor=-4, draw_order=0)
        )
        # 目标单格 (1,?)-... 脚印只有 2x2，取右下角单格 (1,1)-[2,2)，
        # 必须倒推出完整的 4x4 源像素范围
        frags = model.query("c", Rect(1, 1, 2, 2))
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0].page_rect, Rect(4, 4, 8, 8))
        # 不能只给中心单像素
        self.assertNotEqual(frags[0].page_rect, Rect(6, 6, 7, 7))
        # 左下角单格 (0,1)-[1,2) -> 源 [0,4)-[4,8)
        frags = model.query("c", Rect(0, 1, 1, 2))
        self.assertEqual(frags[0].page_rect, Rect(0, 4, 4, 8))

    def test_downscale_full_selection_covers_whole_page(self):
        model = ProvenanceModel()
        model.add_page(PageSource("p", 8, 8))
        model.add_canvas(CanvasSource("c", 2, 2))
        model.add_step(
            Step("down", "c", model.pages["p"], Rect(0, 0, 8, 8),
                 transform=Transform.SCALE, factor=-4, draw_order=0)
        )
        frags = model.query("c", Rect(0, 0, 2, 2))
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0].page_rect, Rect(0, 0, 8, 8))

    def test_downscale_with_nonzero_source_origin(self):
        model = ProvenanceModel()
        model.add_page(PageSource("p", 20, 20))
        model.add_canvas(CanvasSource("c", 2, 2))
        model.add_step(
            Step("down", "c", model.pages["p"], Rect(8, 12, 16, 20),
                 transform=Transform.SCALE, factor=-4, draw_order=0)
        )
        frags = model.query("c", Rect(0, 0, 1, 1))
        self.assertEqual(frags[0].page_rect, Rect(8, 12, 12, 16))
        frags = model.query("c", Rect(1, 1, 2, 2))
        self.assertEqual(frags[0].page_rect, Rect(12, 16, 16, 20))

    def test_upscale_inverse_maps_block_to_source_pixel(self):
        model = ProvenanceModel()
        model.add_page(PageSource("p", 4, 4))
        model.add_canvas(CanvasSource("c", 12, 12))
        model.add_step(
            Step("up", "c", model.pages["p"], Rect(1, 1, 4, 4),
                 transform=Transform.SCALE, factor=3,
                 target_x=3, target_y=6, draw_order=0)
        )
        # 脚印 [3,6)-[12,15)，画布裁到 y=12。取目标 [6,9)-[9,12)
        frags = model.query("c", Rect(6, 9, 9, 12))
        self.assertEqual(len(frags), 1)
        # 局部目标 (3,3)-(6,6) / 3 -> 局部源 (1,1)-(2,2)，加来源原点 (1,1)
        self.assertEqual(frags[0].page_rect, Rect(2, 2, 3, 3))
        self.assertEqual(frags[0].target_rect, Rect(6, 9, 9, 12))

    def test_downscale_then_recompose_roundtrip(self):
        # 页面 -> 缩小到中间画布 -> 中间画布再平移到成品
        model = ProvenanceModel()
        model.add_page(PageSource("p", 16, 16))
        model.add_canvas(CanvasSource("mid", 4, 4))
        model.add_canvas(CanvasSource("final", 20, 20))
        model.add_step(
            Step("shrink", "mid", model.pages["p"], Rect(0, 0, 16, 16),
                 transform=Transform.SCALE, factor=-4, draw_order=0)
        )
        model.add_step(
            Step("place", "final", model.canvases["mid"],
                 Rect(0, 0, 4, 4), target_x=8, target_y=8, draw_order=0)
        )
        frags = model.query("final", Rect(9, 10, 10, 11))
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0].page_id, "p")
        # mid 上 (1,2)-[2,3) -> 源 4x4 完整范围
        self.assertEqual(frags[0].page_rect, Rect(4, 8, 8, 12))
        self.assertEqual(frags[0].step_path, ("place", "shrink"))
        self.assertEqual(frags[0].target_rect, Rect(9, 10, 10, 11))

    def test_indivisible_downscale_rejected(self):
        model = ProvenanceModel()
        model.add_page(PageSource("p", 10, 10))
        model.add_canvas(CanvasSource("c", 5, 5))
        with self.assertRaises(ValueError):
            model.add_step(
                Step("bad", "c", model.pages["p"], Rect(0, 0, 10, 6),
                     transform=Transform.SCALE, factor=-4, draw_order=0)
            )


# --------------------------------------------------------------------------- #
# 旋转
# --------------------------------------------------------------------------- #


class RotationTests(unittest.TestCase):
    def _build(self, transform):
        model = ProvenanceModel()
        # 6 宽 4 高的来源片，取自 10x10 页面
        model.add_page(PageSource("p", 10, 10))
        # 画布足够大，保证任何旋转后的脚印都不被裁切
        model.add_canvas(CanvasSource("c", 40, 40))
        model.add_step(
            Step("rot", "c", model.pages["p"], Rect(2, 1, 8, 5),
                 transform=transform, target_x=3, target_y=4, draw_order=0)
        )
        return model

    def test_footprint_swaps_dimensions_for_quarter_turns(self):
        for tr in (Transform.ROTATE_90, Transform.ROTATE_270):
            model = self._build(tr)
            # 6x4 转 90°/270° 后交换宽高，变成 4x6
            self.assertEqual(
                model.footprint_of("rot"), Rect(3, 4, 7, 10)
            )
        model = self._build(Transform.ROTATE_180)
        # 180° 不交换宽高，仍是 6x4
        self.assertEqual(model.footprint_of("rot"), Rect(3, 4, 9, 8))

    def test_rotate_90_corner_mapping(self):
        model = self._build(Transform.ROTATE_90)
        fp = model.footprint_of("rot")
        # 结果左上角单格：局部 (0,0) 对应源局部 (0,h-1)=(0,3) -> 来源 (2,4)
        frags = model.query("c", Rect(fp.x0, fp.y0, fp.x0 + 1, fp.y0 + 1))
        self.assertEqual(frags[0].page_rect, Rect(2, 4, 3, 5))
        # 结果右下角局部 (3,5) 对应源局部 (w-1,0)=(5,0) -> 来源 (7,1)
        frags = model.query("c", Rect(fp.x1 - 1, fp.y1 - 1, fp.x1, fp.y1))
        self.assertEqual(frags[0].page_rect, Rect(7, 1, 8, 2))

    def test_rotate_180_mapping(self):
        model = self._build(Transform.ROTATE_180)
        fp = model.footprint_of("rot")  # 6x4，w=6,h=4
        # 左上角局部(0,0) -> 源局部 (w-1,h-1)=(5,3) -> 来源 (7,4)
        frags = model.query("c", Rect(fp.x0, fp.y0, fp.x0 + 1, fp.y0 + 1))
        self.assertEqual(frags[0].page_rect, Rect(7, 4, 8, 5))
        # 右下角局部(5,3) -> 源局部 (0,0) -> 来源 (2,1)
        frags = model.query("c", Rect(fp.x1 - 1, fp.y1 - 1, fp.x1, fp.y1))
        self.assertEqual(frags[0].page_rect, Rect(2, 1, 3, 2))

    def test_rotate_270_mapping(self):
        model = self._build(Transform.ROTATE_270)
        fp = model.footprint_of("rot")
        # 左上角局部(0,0) -> 源局部 (w-1,0)=(5,0) -> 来源 (7,1)
        frags = model.query("c", Rect(fp.x0, fp.y0, fp.x0 + 1, fp.y0 + 1))
        self.assertEqual(frags[0].page_rect, Rect(7, 1, 8, 2))
        # 右下角局部(3,5) -> 源局部 (0,h-1)=(0,3) -> 来源 (2,4)
        frags = model.query("c", Rect(fp.x1 - 1, fp.y1 - 1, fp.x1, fp.y1))
        self.assertEqual(frags[0].page_rect, Rect(2, 4, 3, 5))

    def test_full_rotation_roundtrip_recovers_source_rect(self):
        for tr in (
            Transform.ROTATE_90,
            Transform.ROTATE_180,
            Transform.ROTATE_270,
        ):
            with self.subTest(transform=tr):
                model = self._build(tr)
                fp = model.footprint_of("rot")
                frags = model.query(
                    "c", Rect(fp.x0, fp.y0, fp.x1, fp.y1)
                )
                self.assertEqual(len(frags), 1)
                self.assertEqual(frags[0].page_rect, Rect(2, 1, 8, 5))

# --------------------------------------------------------------------------- #
# 递归拼版（画布引用画布）与交错
# --------------------------------------------------------------------------- #


class NestedCanvasTests(unittest.TestCase):
    def test_two_level_nesting_with_rotation_and_scale(self):
        model = ProvenanceModel()
        model.add_page(PageSource("p", 8, 8))
        model.add_canvas(CanvasSource("mid", 4, 4))
        model.add_canvas(CanvasSource("final", 20, 20))
        # 页面缩小 2 倍放进 mid
        model.add_step(
            Step("shrink", "mid", model.pages["p"], Rect(0, 0, 8, 8),
                 transform=Transform.SCALE, factor=-2, draw_order=0)
        )
        # mid 旋转 90° 后放进 final（4x4 旋转后仍是 4x4）
        model.add_step(
            Step("rot", "final", model.canvases["mid"],
                 Rect(0, 0, 4, 4),
                 transform=Transform.ROTATE_90,
                 target_x=5, target_y=6, draw_order=0)
        )
        fp = model.footprint_of("rot")
        frags = model.query(
            "final", Rect(fp.x0, fp.y0, fp.x1, fp.y1)
        )
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0].page_rect, Rect(0, 0, 8, 8))
        self.assertEqual(frags[0].step_path, ("rot", "shrink"))

        # final 上局部 (0,0) 单格：90° 逆映射到 mid 的 (0,3)-[1,4)，
        # 再经 -2 缩小逆映射到页面 [0,6)-[2,8)
        frags = model.query("final", Rect(5, 6, 6, 7))
        self.assertEqual(frags[0].page_rect, Rect(0, 6, 2, 8))

    def test_occlusion_inside_intermediate_canvas(self):
        model = ProvenanceModel()
        model.add_page(PageSource("a", 10, 10))
        model.add_page(PageSource("b", 4, 4))
        model.add_canvas(CanvasSource("mid", 10, 10))
        model.add_canvas(CanvasSource("final", 10, 10))
        model.add_step(
            Step("a_draw", "mid", model.pages["a"],
                 Rect(0, 0, 10, 10), draw_order=0)
        )
        model.add_step(
            Step("b_draw", "mid", model.pages["b"],
                 Rect(0, 0, 4, 4), target_x=3, target_y=3, draw_order=1)
        )
        model.add_step(
            Step("place", "final", model.canvases["mid"],
                 Rect(0, 0, 10, 10), draw_order=0)
        )
        frags = model.query("final", Rect(0, 0, 10, 10))
        # mid 内的覆盖关系在 final 查询中同样成立
        self.assertEqual(len(frags), 5)
        in_hole = model.query("final", Rect(4, 4, 5, 5))
        self.assertEqual(in_hole[0].page_id, "b")
        self.assertEqual(in_hole[0].step_path, ("place", "b_draw"))

    def test_blank_inside_intermediate_blocks_leaf(self):
        model = ProvenanceModel()
        model.add_page(PageSource("p", 10, 10))
        model.add_canvas(CanvasSource("mid", 10, 10))
        model.add_canvas(CanvasSource("final", 10, 10))
        model.add_step(
            Step("ink", "mid", model.pages["p"],
                 Rect(0, 0, 10, 10), draw_order=0)
        )
        # mid 上后画一块“白纸”（用第二个页面模拟可见空白），覆盖中心
        model.add_page(PageSource("white", 4, 4))
        model.add_step(
            Step("paper", "mid", model.pages["white"],
                 Rect(0, 0, 4, 4), target_x=3, target_y=3, draw_order=1)
        )
        model.add_step(
            Step("place", "final", model.canvases["mid"],
                 Rect(0, 0, 10, 10), draw_order=0)
        )
        frags = model.query("final", Rect(3, 3, 7, 7))
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0].page_id, "white")
        # 被白纸盖住的 p 区域不得出现
        self.assertFalse(any(f.page_id == "p" for f in frags))


# --------------------------------------------------------------------------- #
# 半开区间边界与面积守恒
# --------------------------------------------------------------------------- #


class InvariantTests(unittest.TestCase):
    def test_half_open_boundary_pixels_belong_to_one_fragment(self):
        model = ProvenanceModel()
        model.add_page(PageSource("a", 10, 10))
        model.add_page(PageSource("b", 10, 10))
        model.add_canvas(CanvasSource("c", 20, 10))
        model.add_step(
            Step("left", "c", model.pages["a"], Rect(0, 0, 10, 10),
                 target_x=0, target_y=0, draw_order=0)
        )
        model.add_step(
            Step("right", "c", model.pages["b"], Rect(0, 0, 10, 10),
                 target_x=10, target_y=0, draw_order=1)
        )
        # 边界像素列 x=10 属于右侧
        frags = model.query("c", Rect(9, 0, 11, 10))
        d = {f.target_rect.x0: f for f in frags}
        self.assertEqual(d[9].page_id, "a")
        self.assertEqual(d[10].page_id, "b")
        # 两个分片刚好相接，不重不漏
        self.assertEqual(
            sorted(f.target_rect for f in frags),
            [Rect(9, 0, 10, 10), Rect(10, 0, 11, 10)],
        )

    def test_total_visible_area_conserved_under_interleaving(self):
        model = ProvenanceModel()
        for pid in ("a", "b", "c"):
            model.add_page(PageSource(pid, 100, 100))
        model.add_canvas(CanvasSource("cv", 100, 100))
        model.add_step(
            Step("s0", "cv", model.pages["a"], Rect(0, 0, 100, 100),
                 draw_order=0)
        )
        model.add_step(
            Step("s1", "cv", model.pages["b"], Rect(0, 0, 50, 50),
                 target_x=25, target_y=25, draw_order=1)
        )
        model.add_step(
            Step("s2", "cv", model.pages["c"], Rect(0, 0, 20, 20),
                 target_x=40, target_y=40, draw_order=2)
        )
        frags = model.query("cv", Rect(0, 0, 100, 100))
        area = lambda r: (r.x1 - r.x0) * (r.y1 - r.y0)
        # 不重不漏
        self.assertEqual(sum(area(f.target_rect) for f in frags), 10000)
        for i, x in enumerate(frags):
            for y in frags[i + 1:]:
                self.assertIsNone(rect_intersect(x.target_rect, y.target_rect))
        counts = {}
        for f in frags:
            counts[f.page_id] = counts.get(f.page_id, 0) + area(f.target_rect)
        # c 占 400；b 可见部分为 2500-400；其余为 a
        self.assertEqual(counts["c"], 400)
        self.assertEqual(counts["b"], 2500 - 400)
        self.assertEqual(counts["a"], 10000 - 2500)

    def test_fragment_target_rects_stay_inside_selection(self):
        model = ProvenanceModel()
        model.add_page(PageSource("p", 50, 50))
        model.add_canvas(CanvasSource("c", 30, 30))
        model.add_step(
            Step("s", "c", model.pages["p"], Rect(0, 0, 50, 50),
                 target_x=-10, target_y=-10, draw_order=0)
        )
        sel = Rect(5, 5, 25, 25)
        frags = model.query("c", list(sel))
        for f in frags:
            self.assertIsNotNone(rect_intersect(f.target_rect, sel))
            self.assertTrue(
                f.target_rect.x0 >= sel.x0
                and f.target_rect.y0 >= sel.y0
                and f.target_rect.x1 <= sel.x1
                and f.target_rect.y1 <= sel.y1
            )


# --------------------------------------------------------------------------- #
# 校验与错误路径
# --------------------------------------------------------------------------- #


class ValidationTests(unittest.TestCase):
    def test_duplicate_ids_rejected(self):
        model = ProvenanceModel()
        model.add_page(PageSource("x", 5, 5))
        with self.assertRaises(ValueError):
            model.add_canvas(CanvasSource("x", 5, 5))

    def test_unregistered_target_and_source_rejected(self):
        model = ProvenanceModel()
        page = PageSource("p", 5, 5)
        model.add_page(page)
        model.add_canvas(CanvasSource("c", 10, 10))
        with self.assertRaises(ValueError):
            model.add_step(
                Step("s", "ghost", model.pages["p"], Rect(0, 0, 5, 5))
            )
        other_model_page = PageSource("p", 5, 5)
        with self.assertRaises(ValueError):
            model.add_step(
                Step("s2", "c", other_model_page, Rect(0, 0, 5, 5))
            )

    def test_source_rect_out_of_bounds_rejected(self):
        model = ProvenanceModel()
        model.add_page(PageSource("p", 5, 5))
        model.add_canvas(CanvasSource("c", 10, 10))
        with self.assertRaises(ValueError):
            model.add_step(
                Step("s", "c", model.pages["p"], Rect(0, 0, 6, 5))
            )

    def test_duplicate_draw_order_rejected(self):
        model = ProvenanceModel()
        model.add_page(PageSource("p", 5, 5))
        model.add_canvas(CanvasSource("c", 10, 10))
        model.add_step(
            Step("s1", "c", model.pages["p"], Rect(0, 0, 5, 5), draw_order=1)
        )
        with self.assertRaises(ValueError):
            model.add_step(
                Step("s2", "c", model.pages["p"], Rect(0, 0, 5, 5), draw_order=1)
            )

    def test_step_fully_outside_canvas_rejected(self):
        model = ProvenanceModel()
        model.add_page(PageSource("p", 5, 5))
        model.add_canvas(CanvasSource("c", 10, 10))
        with self.assertRaises(ValueError):
            model.add_step(
                Step("s", "c", model.pages["p"], Rect(0, 0, 5, 5),
                     target_x=100, target_y=100)
            )

    def test_scale_factor_validation(self):
        model = ProvenanceModel()
        model.add_page(PageSource("p", 8, 8))
        model.add_canvas(CanvasSource("c", 8, 8))
        with self.assertRaises(ValueError):
            Step(
                "s", "c", model.pages["p"], Rect(0, 0, 8, 8),
                transform=Transform.SCALE, factor=1,
            )
        with self.assertRaises(ValueError):
            Step(
                "s", "c", model.pages["p"], Rect(0, 0, 8, 8),
                transform=Transform.SCALE, factor=0,
            )
        with self.assertRaises(ValueError):
            Step(
                "s", "c", model.pages["p"], Rect(0, 0, 8, 8),
                transform=Transform.IDENTITY, factor=2,
            )

    def test_canvas_cycle_rejected(self):
        model = ProvenanceModel()
        model.add_canvas(CanvasSource("a", 10, 10))
        model.add_canvas(CanvasSource("b", 10, 10))
        model.add_step(
            Step("a_to_b", "b", model.canvases["a"],
                 Rect(0, 0, 5, 5), draw_order=0)
        )
        with self.assertRaises(ValueError):
            model.add_step(
                Step("b_to_a", "a", model.canvases["b"],
                     Rect(0, 0, 5, 5), draw_order=0)
            )

    def test_query_unknown_canvas_and_bad_selection(self):
        model = ProvenanceModel()
        with self.assertRaises(ValueError):
            model.query("nope", Rect(0, 0, 1, 1))
        model.add_canvas(CanvasSource("c", 5, 5))
        with self.assertRaises(ValueError):
            model.query("c", Rect(0, 0, 0, 0))

    def test_blank_step_with_source_rect_rejected(self):
        model = ProvenanceModel()
        model.add_canvas(CanvasSource("c", 5, 5))
        with self.assertRaises(ValueError):
            Step("s", "c", BlankSource(2, 2), Rect(0, 0, 2, 2))


if __name__ == "__main__":
    unittest.main()
