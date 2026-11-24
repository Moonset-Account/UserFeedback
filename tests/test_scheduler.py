"""基础排程、边界条件与三入三出逐拍穷举对照。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_scheduler import Problem, solve, lower_bounds, enumerate_matchings
from switch_scheduler.model import InfeasibleError
from switch_scheduler.scheduler import _rebuild_records

from _brute import brute_force_optimal


class TestBasicScheduling(unittest.TestCase):
    def test_single_edge_single_unit_cold_start(self):
        # 冷启动：建立连线当拍不能发，下一拍才发 -> 2 拍
        p = Problem.build([[1, 0], [0, 0]])
        s = solve(p)
        self.assertEqual(s.num_beats, 2)
        self.assertEqual(s.total_changes, 1)
        self.assertEqual(s.records[0].sends, ())
        self.assertEqual(s.records[0].changed_ports, (0,))
        self.assertEqual(s.records[1].sends, ((0, 0),))

    def test_single_edge_warm_start_one_beat(self):
        # 上帧保留连线：首拍即可发
        p = Problem.build([[1, 0]], previous_matching=(0,))
        s = solve(p)
        self.assertEqual(s.num_beats, 1)
        self.assertEqual(s.total_changes, 0)
        self.assertEqual(s.records[0].sends, ((0, 0),))

    def test_single_edge_three_units_takes_four_beats_cold(self):
        p = Problem.build([[3, 0], [0, 0]])
        s = solve(p)
        # 第 0 拍建连不发，随后 3 拍各发 1
        self.assertEqual(s.num_beats, 4)
        self.assertEqual(s.total_changes, 1)
        totals = sum(len(r.sends) for r in s.records)
        self.assertEqual(totals, 3)

    def test_zero_demand_empty_schedule(self):
        p = Problem.build([[0, 0], [0, 0]])
        s = solve(p)
        self.assertEqual(s.num_beats, 0)
        self.assertEqual(s.total_changes, 0)
        self.assertEqual(s.records, ())

    def test_permutation_matrix_six_ports(self):
        n = 6
        D = [[1 if i == j else 0 for j in range(n)] for i in range(n)]
        p = Problem.build(D)
        s = solve(p)
        self.assertEqual(s.num_beats, 2)
        self.assertEqual(s.total_changes, 6)
        # 第一拍建连、第二拍全发
        self.assertEqual(s.records[0].sends, ())
        self.assertEqual(len(s.records[1].sends), 6)

    def test_partial_change_unchanged_ports_still_send(self):
        # 题面交错规则：仅改动的端口当拍静默，未改连线仍能传
        D = [[1, 0, 0], [0, 1, 0], [1, 0, 0]]
        p = Problem.build(D, previous_matching=(0, 1, -1))
        s = solve(p)
        self.assertEqual(s.num_beats, 3)
        # 第 0 拍 i0/i1 未改，立即发；i2 建立新连线当拍静默
        self.assertEqual(s.records[0].sends, ((0, 0), (1, 1)))
        self.assertEqual(s.records[0].changed_ports, ())
        # 第 1 拍 i2 的新连线尚在建立（与上拍不同），故当拍不发数
        self.assertEqual(s.records[1].sends, ())
        self.assertIn(2, s.records[1].changed_ports)
        # 未改动的端口不应出现在变更集合中
        self.assertNotIn(1, s.records[1].changed_ports)
        # 第 2 拍 i2 用新连线发最后一单位
        self.assertEqual(s.records[2].sends, ((2, 0),))
        # 最少变更：i1 全程保持，只需 i0 断开、i2 建立两次变更
        self.assertEqual(s.total_changes, 2)

    def test_matching_enumeration_lexicographic(self):
        ms = enumerate_matchings(2, 2, ((0, 1), (0, 1)))
        # -1 最小；每个输入位先悬空再按端口顺序
        self.assertEqual(ms[0], (-1, -1))
        self.assertEqual(ms, tuple(sorted(ms)))
        # 输出不重复
        for m in ms:
            used = [j for j in m if j != -1]
            self.assertEqual(len(used), len(set(used)))

    def test_deterministic_tie_break_same_input(self):
        # 对称需求下多次求解结果完全一致（同价按端口顺序固定）
        D = [[1, 1], [1, 1]]
        p = Problem.build(D)
        s1 = solve(p)
        s2 = solve(p)
        self.assertEqual(s1.matchings(), s2.matchings())
        self.assertEqual(s1.total_changes, s2.total_changes)


class TestLoadBoundIsOnlyLowerBound(unittest.TestCase):
    """三入三出小例：逐拍穷举对照，证明行列最大负载只是下界。"""

    def setUp(self):
        # 两个置换叠加：每行每列负载均为 2
        self.D = [
            [1, 1, 0],
            [0, 1, 1],
            [1, 0, 1],
        ]

    def test_load_bound_equals_two(self):
        p = Problem.build(self.D)
        lb = lower_bounds(p)
        self.assertEqual(lb["max_row_load"], 2)
        self.assertEqual(lb["max_col_load"], 2)
        self.assertEqual(lb["load_bound"], 2)

    def test_cold_start_optimal_is_four_beats(self):
        p = Problem.build(self.D)
        s = solve(p)
        # 重配延迟：两个置换各需“建一拍 + 发一拍” -> 4 拍 > 下界 2
        self.assertEqual(s.num_beats, 4)
        # 与独立逐拍穷举完全一致
        bf = brute_force_optimal(p)
        self.assertIsNotNone(bf)
        bf_beats, bf_changes, bf_path = bf
        self.assertEqual(bf_beats, 4)
        self.assertEqual(s.num_beats, bf_beats)
        self.assertEqual(s.total_changes, bf_changes)
        self.assertEqual(s.matchings(), list(bf_path))

    def test_warm_start_optimal_is_three_beats(self):
        # 上帧已保留第一个置换连线：省掉一次建连拍 -> 3 拍，仍 > 下界 2
        p = Problem.build(self.D, previous_matching=(0, 1, 2))
        s = solve(p)
        self.assertEqual(s.num_beats, 3)
        bf = brute_force_optimal(p)
        self.assertIsNotNone(bf)
        self.assertEqual(s.num_beats, bf[0])
        self.assertEqual(s.total_changes, bf[1])
        self.assertEqual(s.matchings(), list(bf[2]))

    def test_rebuild_consistency(self):
        p = Problem.build(self.D)
        s = solve(p)
        records, changes = _rebuild_records(p, tuple(s.matchings()))
        self.assertEqual([r.matching for r in records], s.matchings())
        self.assertEqual(changes, s.total_changes)


class TestForbiddenCrosspoints(unittest.TestCase):
    def test_forbidden_routes_avoided(self):
        D = [[0, 1], [1, 0]]
        p = Problem.build(D, forbidden={(0, 0), (1, 1)})
        s = solve(p)
        for r in s.records:
            for i, j in enumerate(r.matching):
                if j != -1:
                    self.assertNotIn((i, j), p.forbidden)
        sent = sum((r.sends for r in s.records), ())
        self.assertEqual(sorted(sent), [(0, 1), (1, 0)])

    def test_demand_on_forbidden_is_rejected_upfront(self):
        from switch_scheduler.model import IllegalPlanError

        with self.assertRaises(IllegalPlanError):
            Problem.build([[1, 0]], forbidden={(0, 0)})


class TestInfeasibleAndValidation(unittest.TestCase):
    def test_window_smaller_than_demand_static_miss(self):
        # 静态即可判定窗口容量不足，build 阶段直接抛 InfeasibleError 并带错过信息
        with self.assertRaises(InfeasibleError) as ctx:
            Problem.build([[2, 0]], windows={(0, 0): (0, 0)})
        miss = ctx.exception.missed
        self.assertIsNotNone(miss)
        self.assertEqual((miss.i, miss.j), (0, 0))
        self.assertEqual(miss.remaining, 2)
        self.assertEqual(miss.window, (0, 0))

    def test_runtime_miss_reconfig_delay_inside_window(self):
        # 两条边都只能在第 1 拍发，但冷启动第 0 拍建连、第 1 拍才可用，
        # 同一输出冲突使其中一条必然错过窗口
        p = Problem.build(
            [[1, 1, 0], [0, 0, 0]],
            windows={(0, 0): (1, 1), (0, 1): (1, 1)},
        )
        with self.assertRaises(InfeasibleError) as ctx:
            solve(p)
        self.assertIsNotNone(ctx.exception.missed)
        self.assertEqual(ctx.exception.missed.missed_beat, 1)

    def test_invalid_dimensions_rejected(self):
        with self.assertRaises(ValueError):
            Problem.build([[]])
        with self.assertRaises(ValueError):
            Problem.build([[-1, 0]])
        with self.assertRaises(ValueError):
            Problem.build(
                [[1]], previous_matching=(5,)
            )
        with self.assertRaises(ValueError):
            Problem.build([[1, 0]], previous_matching=(0, 0))


if __name__ == "__main__":
    unittest.main()
