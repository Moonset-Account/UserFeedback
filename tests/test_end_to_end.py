"""端到端合成取证场景：把题面所有要素串成一条真实拼版流水线。

流水线：
  原始页 pA/pB/pC
    -> 中间画布 sheet1（pA 缩小 2 倍、pB 旋转 90° 拼上、一张白纸盖角）
    -> 中间画布 sheet2（pC 重复印刷两份）
    -> 成品 final（sheet1 旋转 180° 裁切重拼、sheet2 平移拼上，局部再被盖）

全程仅使用本地合成数据，不访问任何外部资源。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from provenance import (  # noqa: E402
    BlankSource,
    CanvasSource,
    PageSource,
    ProvenanceModel,
    Rect,
    Step,
    Transform,
    rect_intersect,
)


class EndToEndPipelineTests(unittest.TestCase):
    def setUp(self):
        m = ProvenanceModel()

        # ---- 原始页 -------------------------------------------------- #
        m.add_page(PageSource("pA", 16, 16))
        m.add_page(PageSource("pB", 8, 12))
        m.add_page(PageSource("pC", 6, 6))

        # ---- sheet1：缩小 + 旋转 + 白纸盖角 -------------------------- #
        m.add_canvas(CanvasSource("sheet1", 30, 30))
        # pA 16x16 缩小 2 倍 -> 8x8，放在 (0,0)
        m.add_step(
            Step("a_shrink", "sheet1", m.pages["pA"],
                 Rect(0, 0, 16, 16),
                 transform=Transform.SCALE, factor=-2,
                 target_x=0, target_y=0, draw_order=0)
        )
        # pB 取 8x12 整片旋转 90° -> 12x8，放在 (10, 0)
        m.add_step(
            Step("b_rot", "sheet1", m.pages["pB"], Rect(0, 0, 8, 12),
                 transform=Transform.ROTATE_90,
                 target_x=10, target_y=0, draw_order=1)
        )
        # 一张 4x4 白纸盖在 sheet1 的 (2,2)，压住 pA 缩小图的一角
        m.add_step(
            Step("white1", "sheet1", BlankSource(4, 4), None,
                 target_x=2, target_y=2, draw_order=2)
        )

        # ---- sheet2：同一页重复印刷两份 ------------------------------ #
        m.add_canvas(CanvasSource("sheet2", 20, 10))
        m.add_step(
            Step("c_copy1", "sheet2", m.pages["pC"], Rect(0, 0, 6, 6),
                 target_x=0, target_y=2, draw_order=0)
        )
        m.add_step(
            Step("c_copy2", "sheet2", m.pages["pC"], Rect(0, 0, 6, 6),
                 target_x=10, target_y=2, draw_order=1)
        )

        # ---- final：裁切重拼 + 再覆盖 -------------------------------- #
        m.add_canvas(CanvasSource("final", 40, 40))
        # sheet1 取 [0,0)-[20,20) 旋转 180°（仍 20x20）放到 (0,0)
        m.add_step(
            Step("s1_rot180", "final", m.canvases["sheet1"],
                 Rect(0, 0, 20, 20),
                 transform=Transform.ROTATE_180,
                 target_x=0, target_y=0, draw_order=0)
        )
        # sheet2 平移到 (0, 25)
        m.add_step(
            Step("s2_place", "final", m.canvases["sheet2"],
                 Rect(0, 0, 20, 10),
                 target_x=0, target_y=25, draw_order=1)
        )
        # 成品上再盖一张 6x6 白纸，压住重拼后 sheet1 的一块
        m.add_step(
            Step("white_final", "final", BlankSource(6, 6), None,
                 target_x=14, target_y=14, draw_order=2)
        )
        self.model = m

    def test_white_area_on_sheet1_blocks_pa_through_final(self):
        # sheet1 上 (2,2)-[6,6) 被白纸盖住。经 final 的 180° 重拼后，
        # 该区域映射到 final 的 (20-6,20-6)-[20-2,20-2) = (14,14)-[18,18)，
        # 但其中 (14,14)-[20,20) 又被 white_final 整块盖掉，因此直接查
        # white_final 内部必须完全没有叶子片段。
        self.assertEqual(
            self.model.query("final", Rect(15, 15, 19, 19)), []
        )

    def test_pa_downscale_full_source_range_survives_pipeline(self):
        # 实际几何（经调试打印确认）：
        # pA 缩小图在 sheet1 占 [0,0)-[8,8)，被 white1 盖掉 [2,2)-[6,6)；
        # sheet1 选区 20x20 旋转 180° 到 final 后，sheet1 的 (7,7) 格
        # （未被白纸覆盖）映射到 final 的 (12,12) 格。
        # 该格经 -2 缩小逆映射，必须给出完整的 2x2 源范围 [14,14)-[16,16)，
        # 而不是只倒推中心单像素。
        frags = self.model.query("final", Rect(12, 12, 13, 13))
        self.assertEqual(len(frags), 1)
        f = frags[0]
        self.assertEqual(f.page_id, "pA")
        self.assertEqual(f.page_rect, Rect(14, 14, 16, 16))
        self.assertEqual(f.step_path, ("s1_rot180", "a_shrink"))
        # 关键题面要求：不能只倒推中心单像素
        self.assertNotEqual(f.page_rect, Rect(15, 15, 16, 16))

    def test_duplicate_page_keeps_distinct_occurrences(self):
        # sheet2 在 final 上占 [0,25)-[20,35)，两份 pC 分别在
        # x=[0,6) 与 x=[10,16)（y 同为 [27,33)）
        frags = self.model.query("final", Rect(0, 25, 20, 35))
        c_frags = [f for f in frags if f.page_id == "pC"]
        self.assertEqual(len(c_frags), 2)
        target_x0s = sorted(f.target_rect.x0 for f in c_frags)
        self.assertEqual(target_x0s, [0, 10])
        paths = sorted(f.step_path for f in c_frags)
        self.assertEqual(
            paths,
            [("s2_place", "c_copy1"), ("s2_place", "c_copy2")],
        )
        # 两份的来源矩形都是 pC 整片
        self.assertTrue(
            all(f.page_rect == Rect(0, 0, 6, 6) for f in c_frags)
        )

    def test_rotation_chain_corner_provenance(self):
        # pB 在 sheet1 上旋转 90° 后占 [10,0)-[22,8)；再随 sheet1 180°
        # 到 final。sheet1 点 (10,0) 经 180° -> final (20-10-? ...)
        # 直接用“整片可溯源”验证：查 final 中对应 pB 的区域，
        # 至少能取回一个 page_id=pB 且路径正确的片段，来源矩形落在 pB 内。
        # sheet1 [10,0)-[22,8) 经 180°（相对 20x20 选区）->
        # final [20-22, 20-8)-[20-10,20-0) = 负 x 被画布裁掉，
        # 实际可见为 final [0,12)-[10,20)。
        frags = self.model.query("final", Rect(0, 12, 10, 20))
        pb = [f for f in frags if f.page_id == "pB"]
        self.assertTrue(pb)
        for f in pb:
            self.assertEqual(f.step_path[0], "s1_rot180")
            self.assertEqual(f.step_path[1], "b_rot")
            # 来源坐标必须在 pB 的 8x12 页面范围内
            self.assertGreaterEqual(f.page_rect.x0, 0)
            self.assertGreaterEqual(f.page_rect.y0, 0)
            self.assertLessEqual(f.page_rect.x1, 8)
            self.assertLessEqual(f.page_rect.y1, 12)

    def test_half_open_boundary_no_double_count(self):
        # sheet2 上两份 pC 各占 6 宽：copy1 在 final x=[0,6)，copy2 在
        # x=[10,16)，中间 [6,10) 是没有内容的空隙。半开边界下：
        #   x=5 列归 copy1，x=6 列是空的，x=10 列归 copy2。
        col5 = self.model.query("final", Rect(5, 27, 6, 33))
        col6_gap = self.model.query("final", Rect(6, 27, 7, 33))
        col10 = self.model.query("final", Rect(10, 27, 11, 33))

        self.assertEqual(len(col5), 1)
        self.assertEqual(col5[0].step_path, ("s2_place", "c_copy1"))
        self.assertEqual(col5[0].target_rect, Rect(5, 27, 6, 33))

        self.assertEqual(col6_gap, [])  # 半开：copy1 的右界之外即空隙

        self.assertEqual(len(col10), 1)
        self.assertEqual(col10[0].step_path, ("s2_place", "c_copy2"))
        self.assertEqual(col10[0].target_rect.x0, 10)
        # 边界列 x=10 归第二份，且两份的目标矩形互不重叠
        self.assertLess(col5[0].target_rect.x1, col10[0].target_rect.x0)

    def test_area_conservation_on_final(self):
        area = lambda r: (r.x1 - r.x0) * (r.y1 - r.y0)

        # 独立测量两个来源表面在各自选区中的可见面积。
        sheet1_visible = sum(
            area(f.target_rect)
            for f in self.model.query("sheet1", Rect(0, 0, 20, 20))
        )
        sheet2_visible = sum(
            area(f.target_rect)
            for f in self.model.query("sheet2", Rect(0, 0, 20, 10))
        )
        # sheet1：pA 缩小 8x8=64；pB 旋转后 12x8 但被 20 宽选区裁掉 2 列
        # （16 像素）-> 80；再被 white1 盖掉 4x4=16（全在 pA 上）。
        self.assertEqual(sheet1_visible, 64 + 80 - 16)
        # sheet2：两份 6x6，共 72
        self.assertEqual(sheet2_visible, 72)

        # 对照模型：与主模型完全相同，但不添加 final 上的 white_final。
        # 用独立重放而非同模型推算，避免循环论证。
        ref = self._build_reference_without_final_blank()
        ref_frags = ref.query("final", Rect(0, 0, 40, 40))
        ref_visible = sum(area(f.target_rect) for f in ref_frags)
        self.assertEqual(ref_visible, sheet1_visible + sheet2_visible)

        blank_fp = self.model.footprint_of("white_final")
        # 白纸真正遮盖的“有内容”面积 = 对照模型在白纸脚印内的可见面积
        covered = 0
        for f in ref_frags:
            hit = rect_intersect(f.target_rect, blank_fp)
            if hit is not None:
                covered += area(hit)
        self.assertGreater(covered, 0)
        self.assertLessEqual(covered, area(blank_fp))  # 空白处不算遮盖量

        frags = self.model.query("final", Rect(0, 0, 40, 40))
        # 所有片段互不相交（半开区间不重不漏）
        for i, a in enumerate(frags):
            for b in frags[i + 1:]:
                self.assertIsNone(rect_intersect(a.target_rect, b.target_rect))
        # 加白纸后可见面积恰好减少 covered
        visible = sum(area(f.target_rect) for f in frags)
        self.assertEqual(visible, ref_visible - covered)

    def _build_reference_without_final_blank(self):
        m = ProvenanceModel()
        m.add_page(PageSource("pA", 16, 16))
        m.add_page(PageSource("pB", 8, 12))
        m.add_page(PageSource("pC", 6, 6))
        m.add_canvas(CanvasSource("sheet1", 30, 30))
        m.add_step(Step("a_shrink", "sheet1", m.pages["pA"],
                        Rect(0, 0, 16, 16),
                        transform=Transform.SCALE, factor=-2, draw_order=0))
        m.add_step(Step("b_rot", "sheet1", m.pages["pB"], Rect(0, 0, 8, 12),
                        transform=Transform.ROTATE_90,
                        target_x=10, target_y=0, draw_order=1))
        m.add_step(Step("white1", "sheet1", BlankSource(4, 4), None,
                        target_x=2, target_y=2, draw_order=2))
        m.add_canvas(CanvasSource("sheet2", 20, 10))
        m.add_step(Step("c_copy1", "sheet2", m.pages["pC"], Rect(0, 0, 6, 6),
                        target_x=0, target_y=2, draw_order=0))
        m.add_step(Step("c_copy2", "sheet2", m.pages["pC"], Rect(0, 0, 6, 6),
                        target_x=10, target_y=2, draw_order=1))
        m.add_canvas(CanvasSource("final", 40, 40))
        m.add_step(Step("s1_rot180", "final", m.canvases["sheet1"],
                        Rect(0, 0, 20, 20),
                        transform=Transform.ROTATE_180, draw_order=0))
        m.add_step(Step("s2_place", "final", m.canvases["sheet2"],
                        Rect(0, 0, 20, 10),
                        target_x=0, target_y=25, draw_order=1))
        return m

    def test_selection_entirely_on_blank_returns_empty(self):
        self.assertEqual(
            self.model.query("final", Rect(14, 14, 20, 20)), []
        )

    def test_out_of_bounds_selection_clipped_not_error(self):
        # 完全越界
        self.assertEqual(
            self.model.query("final", Rect(100, 100, 200, 200)), []
        )
        # 部分越界仍能正常返回且片段都在画布内
        frags = self.model.query("final", Rect(-50, -50, 5, 5))
        for f in frags:
            self.assertGreaterEqual(f.target_rect.x0, 0)
            self.assertGreaterEqual(f.target_rect.y0, 0)


if __name__ == "__main__":
    unittest.main()
