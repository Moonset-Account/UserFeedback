"""满规模（最多 6 入 6 出）与确定性/目标优先级测试。"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_scheduler import Problem, solve, replay
from switch_scheduler.scheduler import lower_bounds


class TestSixBySix(unittest.TestCase):
    def test_full_permutation_cold_start(self):
        n = 6
        D = [[1 if i == j else 0 for j in range(n)] for i in range(n)]
        p = Problem.build(D)
        s = solve(p)
        self.assertEqual(s.num_beats, 2)
        self.assertEqual(s.total_changes, 6)
        rr = replay(p, s.matchings())
        self.assertTrue(rr.cleared)

    def test_two_permutations_six_ports(self):
        # 两个不相交置换叠加，每行每列负载 2
        D = [[0] * 6 for _ in range(6)]
        for i in range(6):
            D[i][i] += 1
            D[i][(i + 1) % 6] += 1
        p = Problem.build(D)
        lb = lower_bounds(p)
        self.assertEqual(lb["load_bound"], 2)
        start = time.perf_counter()
        s = solve(p)
        elapsed = time.perf_counter() - start
        # 冷启动两个置换各需建+发，共 4 拍；再次体现 2 只是下界
        self.assertEqual(s.num_beats, 4)
        self.assertLess(elapsed, 10.0)
        rr = replay(p, s.matchings())
        self.assertTrue(rr.cleared)
        # 每拍每个输入/输出至多参与一次
        for r in s.records:
            inputs = [i for i, j in enumerate(r.matching) if j != -1]
            outputs = [j for j in r.matching if j != -1]
            self.assertEqual(len(inputs), len(set(inputs)))
            self.assertEqual(len(outputs), len(set(outputs)))

    def test_warm_matching_beats_cold_on_changes(self):
        # 同样需求下，保留连线应得到更少/相等的变更与拍数
        n = 4
        D = [[1 if i == j else 0 for j in range(n)] for i in range(n)]
        cold = solve(Problem.build(D))
        warm = solve(Problem.build(D, previous_matching=tuple(range(n))))
        self.assertLessEqual(warm.num_beats, cold.num_beats)
        self.assertLess(warm.total_changes, cold.total_changes)
        self.assertEqual(warm.num_beats, 1)
        self.assertEqual(warm.total_changes, 0)

    def test_min_changes_tie_break(self):
        # 单条边冷启动：最优必须只建一次连线（变更 1），而非反复改线
        p = Problem.build([[0, 0, 2], [0, 0, 0]])
        s = solve(p)
        self.assertEqual(s.total_changes, 1)
        matchings = s.matchings()
        self.assertTrue(all(m == matchings[0] for m in matchings))

    def test_deterministic_across_repeated_runs(self):
        D = [
            [1, 1, 0, 0],
            [0, 1, 1, 0],
            [1, 0, 0, 1],
            [0, 0, 1, 1],
        ]
        p = Problem.build(D)
        first = solve(p)
        for _ in range(3):
            again = solve(p)
            self.assertEqual(first.matchings(), again.matchings())
            self.assertEqual(first.total_changes, again.total_changes)

    def test_replay_independently_clears_full_schedule(self):
        D = [
            [2, 1, 0],
            [0, 1, 2],
            [1, 0, 1],
        ]
        p = Problem.build(D)
        s = solve(p)
        rr = replay(p, s.matchings())
        self.assertTrue(rr.cleared)
        # 完成量逐项等于需求
        for i in range(3):
            for j in range(3):
                self.assertEqual(rr.completed[i][j], D[i][j])
        # 变更统计与记录一致
        self.assertEqual(
            s.total_changes, sum(len(r.changed_ports) for r in s.records)
        )


if __name__ == "__main__":
    unittest.main()
