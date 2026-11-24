"""回放合法性校验与读后重排（交错场景）测试。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_scheduler import Problem, solve, replay, replan
from switch_scheduler.model import IllegalPlanError, InfeasibleError


def _send_edges(schedule):
    return [list(r.sends) for r in schedule.records]


class TestReplay(unittest.TestCase):
    def test_replay_optimal_schedule_clears_matrix(self):
        D = [[1, 1, 0], [0, 1, 1], [1, 0, 1]]
        p = Problem.build(D)
        s = solve(p)
        rr = replay(p, s.matchings(), _send_edges(s))
        self.assertTrue(rr.cleared)
        self.assertEqual(rr.completed, tuple(tuple(row) for row in D))
        self.assertEqual(rr.num_beats, s.num_beats)
        self.assertIsNone(rr.first_missed)
        # 记录与求解器给出的逐拍记录一致
        for got, want in zip(rr.records, s.records):
            self.assertEqual(got.matching, want.matching)
            self.assertEqual(got.sends, want.sends)
            self.assertEqual(got.changed_ports, want.changed_ports)

    def test_replay_default_sends_match_engine(self):
        p = Problem.build([[2, 0], [0, 1]])
        s = solve(p)
        rr = replay(p, s.matchings())
        self.assertTrue(rr.cleared)

    def test_send_on_changed_port_is_illegal(self):
        p = Problem.build([[1, 0]], previous_matching=(-1,))
        with self.assertRaises(IllegalPlanError):
            replay(p, [(0,)], declared_sends=[[(0, 0)]])

    def test_oversend_is_illegal(self):
        p = Problem.build([[1, 0]])
        # 第 0 拍建连不发；第 1 拍发；第 2 拍再声明发即超量
        with self.assertRaises(IllegalPlanError):
            replay(
                p,
                [(0,), (0,), (0,)],
                declared_sends=[[], [(0, 0)], [(0, 0)]],
            )

    def test_send_outside_window_is_illegal(self):
        # 窗口只在第 1 拍开放，第 2 拍声明发送必须被判非法
        p = Problem.build([[1, 0]], windows={(0, 0): (1, 1)})
        with self.assertRaises(IllegalPlanError):
            replay(
                p,
                [(0,), (0,), (0,)],
                declared_sends=[[], [(0, 0)], [(0, 0)]],
            )

    def test_invalid_matching_rejected(self):
        p = Problem.build([[1, 1], [1, 0]])
        # 两个输入连同一输出
        with self.assertRaises(IllegalPlanError):
            replay(p, [(0, 0)], declared_sends=[[]])
        # 命中不可用交叉点
        p2 = Problem.build([[0, 1], [1, 0]], forbidden={(0, 0)})
        with self.assertRaises(IllegalPlanError):
            replay(p2, [(0, 1)], declared_sends=[[]])

    def test_idle_hold_replayed_without_sending(self):
        # 显式留空：可发的边不发，后续再发，矩阵最终仍能清空
        p = Problem.build([[2, 0]])
        s = solve(p)  # 冷启动 3 拍：建、发、发
        base = s.matchings()
        matchings = base + [base[-1]]
        sends = [list(s.records[0].sends), list(s.records[1].sends), [], [(0, 0)]]
        rr = replay(p, matchings, sends)
        self.assertTrue(rr.cleared)
        # 第 2 拍被标记为留空
        self.assertIn((0, 0), rr.records[2].idle_edges)

    def test_first_missed_reported(self):
        # 构造一个必然错过窗口的回放
        p = Problem.build([[1, 0]], windows={(0, 0): (0, 0)})
        # 冷启动第 0 拍才建连，当拍不能发 -> 窗口关闭
        rr = replay(p, [(0,)], declared_sends=[[]])
        self.assertFalse(rr.cleared)
        self.assertIsNotNone(rr.first_missed)
        self.assertEqual(rr.first_missed.window, (0, 0))
        self.assertEqual(rr.first_missed.remaining, 1)


class TestReplan(unittest.TestCase):
    def test_replan_after_partial_actual_completion(self):
        D = [[2, 0], [0, 2]]
        p = Problem.build(D)
        full = solve(p)
        # 设备执行第一拍（建连，无发数）后回报：什么都没完成
        frozen, suffix = replan(
            p,
            [full.records[0].matching],
            actual_completion=[[0, 0], [0, 0]],
        )
        self.assertEqual(frozen.num_beats, 1)
        # 后缀从真实连线继续，窗口拍号按绝对拍号对齐
        self.assertEqual(suffix.records[0].beat, 1)
        combined = [r.matching for r in frozen.records] + [
            r.matching for r in suffix.records
        ]
        rr = replay(p, combined)
        self.assertTrue(rr.cleared)

    def test_replan_with_skewed_completion_uses_real_remaining(self):
        # 计划外的真实完成：一条边已送完、另一条原封不动
        D = [[1, 0], [0, 1]]
        p = Problem.build(D, previous_matching=(0, 1))
        frozen, suffix = replan(
            p,
            [(0, 1)],
            actual_completion=[[1, 0], [0, 0]],
        )
        # 后缀只需完成 (1,1)；连线保持，一拍即可且零变更
        self.assertEqual(suffix.num_beats, 1)
        self.assertEqual(suffix.total_changes, 0)
        self.assertEqual(suffix.records[0].sends, ((1, 1),))
        rr = replay(
            p,
            [r.matching for r in frozen.records]
            + [r.matching for r in suffix.records],
        )
        self.assertTrue(rr.cleared)

    def test_replan_window_alignment_absolute_beats(self):
        # 窗口 [1,2]：冻结 1 拍后子问题窗口平移为 [0,1]
        p = Problem.build(
            [[1, 0]], windows={(0, 0): (1, 2)}
        )
        full = solve(p)
        self.assertGreaterEqual(full.num_beats, 2)
        frozen, suffix = replan(
            p,
            [full.records[0].matching],
            actual_completion=[[0, 0]],
        )
        # 子问题里窗口左沿已平移到 0，第 1 绝对拍（子问题第 0 拍）即可发
        self.assertEqual(suffix.records[0].sends, ((0, 0),))
        combined = [r.matching for r in frozen.records] + [
            r.matching for r in suffix.records
        ]
        rr = replay(p, combined)
        self.assertTrue(rr.cleared)

    def test_replan_infeasible_raises_with_missed(self):
        p = Problem.build([[1, 0]], windows={(0, 0): (0, 0)})
        with self.assertRaises(InfeasibleError):
            replan(p, [(-1,)], actual_completion=[[0, 0]])

    def test_actual_completion_validation(self):
        p = Problem.build([[1, 0]])
        with self.assertRaises(IllegalPlanError):
            replan(p, [(-1,)], actual_completion=[[2, 0]])  # 超过原始需求
        with self.assertRaises(IllegalPlanError):
            replan(p, [(-1,)], actual_completion=[[-1, 0]])

    def test_replan_zero_frozen_beats_uses_previous_matching(self):
        # 零冻结拍：从保留连线直接重排，等价于求解原问题
        D = [[1, 0], [0, 1]]
        p = Problem.build(D, previous_matching=(0, 1))
        frozen, suffix = replan(
            p, [], actual_completion=[[0, 0], [0, 0]]
        )
        self.assertEqual(frozen.num_beats, 0)
        self.assertEqual(suffix.num_beats, 1)
        self.assertEqual(suffix.records[0].beat, 0)
        rr = replay(p, [r.matching for r in suffix.records])
        self.assertTrue(rr.cleared)

    def test_replan_empty_suffix_when_already_cleared(self):
        # 冻结前缀后设备已完成全部需求：后缀为空排程
        D = [[1, 0], [0, 1]]
        p = Problem.build(D, previous_matching=(0, 1))
        frozen, suffix = replan(
            p, [(0, 1)], actual_completion=[[1, 0], [0, 1]]
        )
        self.assertEqual(frozen.num_beats, 1)
        self.assertEqual(suffix.num_beats, 0)
        self.assertEqual(suffix.records, ())
        self.assertEqual(suffix.total_changes, 0)


if __name__ == "__main__":
    unittest.main()
