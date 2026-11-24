"""业务允许拍区间（窗口）边界场景测试。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_scheduler import Problem, solve, replay
from switch_scheduler.model import InfeasibleError
from switch_scheduler.scheduler import lower_bounds


class TestWindowBoundaries(unittest.TestCase):
    def test_preconnect_before_window_opens(self):
        # 边 (0,0) 窗口 [1,1]：第 0 拍预先建连（不发数），第 1 拍立即发
        p = Problem.build(
            [[1, 0], [0, 1]],
            windows={(0, 0): (1, 1), (1, 1): (0, 1)},
        )
        s = solve(p)
        self.assertEqual(s.num_beats, 2)
        self.assertEqual(s.records[0].sends, ())  # 窗口未开，不提前送
        self.assertEqual(s.records[1].sends, ((0, 0), (1, 1)))
        rr = replay(p, s.matchings())
        self.assertTrue(rr.cleared)

    def test_no_early_send_inside_window_only(self):
        # 窗口 [2,2]：前两拍任何发数都被禁止，第 2 拍发
        p = Problem.build([[1, 0]], windows={(0, 0): (2, 2)})
        s = solve(p)
        for r in s.records[:-1]:
            self.assertNotIn((0, 0), r.sends)
        self.assertEqual(s.records[-1].sends, ((0, 0),))
        self.assertEqual(s.records[-1].beat, 2)
        rr = replay(p, s.matchings())
        self.assertTrue(rr.cleared)

    def test_multi_unit_spans_whole_window(self):
        # 需求 3，窗口 [1,3]：预建 + 三拍发送，恰好第 3 拍清空
        p = Problem.build([[3, 0]], windows={(0, 0): (1, 3)})
        s = solve(p)
        self.assertEqual(s.num_beats, 4)
        send_beats = [r.beat for r in s.records if r.sends]
        self.assertEqual(send_beats, [1, 2, 3])

    def test_window_tight_but_feasible_with_warm_matching(self):
        # 热连线 + 窗口 [0,1] + 需求 2：两拍各发 1，恰好可行
        p = Problem.build(
            [[2, 0]],
            windows={(0, 0): (0, 1)},
            previous_matching=(0,),
        )
        s = solve(p)
        self.assertEqual(s.num_beats, 2)
        self.assertEqual(s.total_changes, 0)

    def test_window_missed_when_cold_start_inside_single_beat(self):
        # 冷启动且窗口只开第 0 拍：建连拍无法发数，必然错过
        p = Problem.build([[1, 0]], windows={(0, 0): (0, 0)})
        with self.assertRaises(InfeasibleError) as ctx:
            solve(p)
        miss = ctx.exception.missed
        self.assertEqual((miss.i, miss.j), (0, 0))
        self.assertEqual(miss.missed_beat, 0)
        self.assertEqual(miss.remaining, 1)

    def test_conflicting_windows_one_must_miss(self):
        # 同一输入两条边窗口都只开第 1 拍，一拍只能沿一条连线发
        p = Problem.build(
            [[1, 1]],
            windows={(0, 0): (1, 1), (0, 1): (1, 1)},
        )
        with self.assertRaises(InfeasibleError) as ctx:
            solve(p)
        self.assertIsNotNone(ctx.exception.missed)
        # 首个错过需求按 (关闭拍, 输入, 输出) 固定
        self.assertEqual(ctx.exception.missed.missed_beat, 1)

    def test_window_bound_dominates_load_bound(self):
        # 行列负载下界为 1，但窗口把可行拍数推高，证明负载下界可被窗口突破
        p = Problem.build(
            [[1, 0], [0, 1]],
            windows={(0, 0): (2, 2), (1, 1): (2, 2)},
        )
        lb = lower_bounds(p)
        self.assertEqual(lb["load_bound"], 1)
        s = solve(p)
        self.assertGreater(s.num_beats, lb["load_bound"])
        self.assertEqual(s.records[-1].beat, 2)
        rr = replay(p, s.matchings())
        self.assertTrue(rr.cleared)

    def test_default_window_fills_missing_entries(self):
        p = Problem.build([[1, 0], [0, 1]])
        # 未显式给窗口的正需求自动补默认窗口，求解正常
        s = solve(p)
        rr = replay(p, s.matchings())
        self.assertTrue(rr.cleared)


if __name__ == "__main__":
    unittest.main()
