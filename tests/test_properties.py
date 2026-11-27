"""固定种子的随机性质测试（仅用标准库 random，不引入第三方依赖）。

验证两类不变量：
1. 对每种变换，任意对齐的来源矩形做“正向投影 -> 逆向映射”都能还原；
2. 任意选区查询结果的目标分片互不重叠，且并集恰好等于选区与所有脚印
   并集（再扣除被覆盖部分）的交集。
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from provenance import (  # noqa: E402
    CanvasSource,
    PageSource,
    ProvenanceModel,
    Rect,
    Step,
    Transform,
    _invert_rect,
    _project_rect,
    rect_intersect,
    rect_subtract,
)


def _area(r):
    return (r.x1 - r.x0) * (r.y1 - r.y0)


class TransformRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(20261128)

    def _make_step(self, transform, factor, sw, sh):
        model = ProvenanceModel()
        model.add_page(PageSource("p", sw, sh))
        # 画布足够大，避免裁切干扰纯映射验证
        model.add_canvas(CanvasSource("c", 10_000, 10_000))
        step = Step(
            "s", "c", model.pages["p"], Rect(0, 0, sw, sh),
            transform=transform, factor=factor,
            target_x=123, target_y=456, draw_order=0,
        )
        model.add_step(step)
        return model, step

    def test_identity_and_rotations_roundtrip(self):
        rng = self.rng
        for tr in (
            Transform.IDENTITY,
            Transform.ROTATE_90,
            Transform.ROTATE_180,
            Transform.ROTATE_270,
        ):
            sw, sh = 12, 9
            model, step = self._make_step(tr, None, sw, sh)
            fp = model.footprint_of("s")
            for _ in range(200):
                # 在脚印内随机取非空矩形
                x0 = rng.randrange(fp.x0, fp.x1)
                y0 = rng.randrange(fp.y0, fp.y1)
                x1 = rng.randrange(x0 + 1, fp.x1 + 1)
                y1 = rng.randrange(y0 + 1, fp.y1 + 1)
                target = Rect(x0, y0, x1, y1)
                source = _invert_rect(step, target)
                back = _project_rect(step, source)
                self.assertEqual(
                    back, target,
                    msg=f"{tr}: 正反映射不互逆 target={target} "
                        f"source={source} back={back}",
                )
                # 逆映射得到的来源矩形必须在页面范围内
                self.assertGreaterEqual(source.x0, 0)
                self.assertGreaterEqual(source.y0, 0)
                self.assertLessEqual(source.x1, sw)
                self.assertLessEqual(source.y1, sh)

    def test_downscale_inverse_covers_full_source_cells(self):
        rng = self.rng
        k = 4
        sw = sh = 4 * 8  # 32，可被 k 整除
        model, step = self._make_step(Transform.SCALE, -k, sw, sh)
        fp = model.footprint_of("s")  # 8x8
        for _ in range(200):
            x0 = rng.randrange(fp.x0, fp.x1)
            y0 = rng.randrange(fp.y0, fp.y1)
            x1 = rng.randrange(x0 + 1, fp.x1 + 1)
            y1 = rng.randrange(y0 + 1, fp.y1 + 1)
            target = Rect(x0, y0, x1, y1)
            source = _invert_rect(step, target)
            # 题面核心要求：来源范围的宽高必须是目标格宽高的 k 倍，
            # 即每个目标格都展开成完整的 k x k 源像素，而不是中心单像素。
            self.assertEqual(source.x1 - source.x0, (x1 - x0) * k)
            self.assertEqual(source.y1 - source.y0, (y1 - y0) * k)
            # 边界必须落在 k 的倍数网格上
            self.assertEqual(source.x0 % k, 0)
            self.assertEqual(source.y0 % k, 0)
            # 正向投影该完整来源范围，应当恰好覆盖原目标格（不重不漏）
            back = _project_rect(step, source)
            self.assertEqual(back, target)

    def test_upscale_roundtrip_on_aligned_grid(self):
        rng = self.rng
        s = 3
        sw = sh = 12
        model, step = self._make_step(Transform.SCALE, s, sw, sh)
        fp = model.footprint_of("s")  # 36x36
        for _ in range(200):
            # 只在 s 对齐网格上取矩形，模拟真实可溯源的目标块
            gx0 = rng.randrange(0, sw)
            gy0 = rng.randrange(0, sh)
            gx1 = rng.randrange(gx0 + 1, sw + 1)
            gy1 = rng.randrange(gy0 + 1, sh + 1)
            target = Rect(
                fp.x0 + gx0 * s, fp.y0 + gy0 * s,
                fp.x0 + gx1 * s, fp.y0 + gy1 * s,
            )
            source = _invert_rect(step, target)
            self.assertEqual(
                source, Rect(gx0, gy0, gx1, gy1)
            )
            self.assertEqual(_project_rect(step, source), target)


class QueryPartitionTests(unittest.TestCase):
    def test_random_occluders_partition_selection(self):
        rng = random.Random(777)
        model = ProvenanceModel()
        n_pages = 6
        for i in range(n_pages):
            model.add_page(PageSource(f"p{i}", 40, 40))
        model.add_canvas(CanvasSource("cv", 100, 100))

        placed = []
        order = 0
        for i in range(15):
            pid = f"p{rng.randrange(n_pages)}"
            w = rng.randrange(5, 30)
            h = rng.randrange(5, 30)
            x0 = rng.randrange(0, 40 - w + 1) if w < 40 else 0
            y0 = rng.randrange(0, 40 - h + 1) if h < 40 else 0
            tx = rng.randrange(-10, 90)
            ty = rng.randrange(-10, 90)
            try:
                step = Step(
                    f"st{i}", "cv", model.pages[pid],
                    Rect(x0, y0, x0 + w, y0 + h),
                    target_x=tx, target_y=ty, draw_order=order,
                )
                model.add_step(step)
            except ValueError:
                continue  # 完全越界或次序冲突就跳过
            placed.append(step)
            order += 1

        self.assertGreater(len(placed), 5)  # 确认确实建成了交错场景

        for _ in range(300):
            sx0 = rng.randrange(-20, 110)
            sy0 = rng.randrange(-20, 110)
            sx1 = rng.randrange(sx0 + 1, 130)
            sy1 = rng.randrange(sy0 + 1, 130)
            sel = Rect(sx0, sy0, sx1, sy1)
            frags = model.query("cv", sel)

            # 1) 所有分片都在选区内
            for f in frags:
                self.assertIsNotNone(rect_intersect(f.target_rect, sel))
                self.assertGreaterEqual(f.target_rect.x0, max(sel.x0, 0))
                self.assertGreaterEqual(f.target_rect.y0, max(sel.y0, 0))
                self.assertLessEqual(f.target_rect.x1, min(sel.x1, 100))
                self.assertLessEqual(f.target_rect.y1, min(sel.y1, 100))

            # 2) 分片两两不相交
            for i, a in enumerate(frags):
                for b in frags[i + 1:]:
                    self.assertIsNone(
                        rect_intersect(a.target_rect, b.target_rect),
                        msg=f"分片重叠: {a.target_rect} vs {b.target_rect}",
                    )

            # 3) 分片并集 == 选区与“最上层可见内容”的交集。
            # 独立用自顶向下覆盖模拟一遍期望覆盖面积。
            canvas_clip = rect_intersect(sel, Rect(0, 0, 100, 100))
            if canvas_clip is None:
                self.assertEqual(frags, [])
                continue
            covered = []  # 已被任意步骤脚印盖住的区域
            remaining = [canvas_clip]
            for step in reversed(model.steps_on("cv")):
                fp = model.footprint_of(step.step_id)
                new_remaining = []
                for piece in remaining:
                    hit = rect_intersect(piece, fp)
                    if hit is None:
                        new_remaining.append(piece)
                    else:
                        new_remaining.extend(
                            rect_subtract(piece, hit)
                        )
                        covered.append(hit)
                remaining = new_remaining
            expected_area = sum(_area(r) for r in covered)
            self.assertEqual(
                sum(_area(f.target_rect) for f in frags),
                expected_area,
                msg=f"选区 {sel} 的可见面积不守恒",
            )


if __name__ == "__main__":
    unittest.main()
