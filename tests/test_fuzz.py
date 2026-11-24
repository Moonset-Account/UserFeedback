"""随机小规模实例：求解器与独立逐拍穷举逐项对照。"""

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_scheduler import Problem, solve, replay
from switch_scheduler.model import InfeasibleError

from _brute import brute_force_optimal


def _random_problem(rng: random.Random, n_in: int, n_out: int):
    demand = [[rng.randint(0, 2) for _ in range(n_out)] for _ in range(n_in)]
    forbidden = set()
    # 随机挑若干零需求交叉点禁用
    for i in range(n_in):
        for j in range(n_out):
            if demand[i][j] == 0 and rng.random() < 0.2:
                forbidden.add((i, j))
    windows = {}
    for i in range(n_in):
        for j in range(n_out):
            if demand[i][j] > 0 and rng.random() < 0.5:
                l = rng.randint(0, 2)
                # 窗口容量必须装得下该边需求，避免静态不可行业务
                r = l + rng.randint(demand[i][j] - 1, demand[i][j] + 2)
                windows[(i, j)] = (l, r)
    prev = tuple(-1 for _ in range(n_in))
    if rng.random() < 0.5:
        # 随机保留一个不冲突的部分匹配
        cols = list(range(n_out))
        rng.shuffle(cols)
        picked = {}
        for i in range(n_in):
            if rng.random() < 0.5 and cols:
                j = cols.pop()
                if (i, j) not in forbidden:
                    picked[i] = j
        prev = tuple(picked.get(i, -1) for i in range(n_in))
    return Problem.build(
        demand, forbidden=forbidden, windows=windows, previous_matching=prev
    )


class TestFuzzAgainstBruteForce(unittest.TestCase):
    def test_2x2_random_instances(self):
        rng = random.Random(20261124)
        checked = 0
        for _ in range(60):
            p = _random_problem(rng, 2, 2)
            bf = brute_force_optimal(p)
            try:
                s = solve(p)
            except InfeasibleError:
                self.assertIsNone(bf, "求解器判不可行，但穷举找到了可行解")
                continue
            self.assertIsNotNone(bf, "求解器给出方案，但穷举判不可行")
            bf_beats, bf_changes, bf_path = bf
            self.assertEqual(s.num_beats, bf_beats)
            self.assertEqual(s.total_changes, bf_changes)
            self.assertEqual(s.matchings(), list(bf_path))
            # 回放必须真实清空
            rr = replay(p, s.matchings())
            self.assertTrue(rr.cleared)
            checked += 1
        self.assertGreater(checked, 20)

    def test_3x3_random_instances(self):
        rng = random.Random(424242)
        checked = 0
        for _ in range(25):
            p = _random_problem(rng, 3, 3)
            # 控制穷举规模：需求总量过大时跳过穷举，仅做回放校验
            if p.total_demand() > 7:
                try:
                    s = solve(p)
                except InfeasibleError:
                    # 随机窗口冲突可导致真实不可行，与穷举结论交叉确认
                    self.assertIsNone(brute_force_optimal(p))
                    continue
                rr = replay(p, s.matchings())
                self.assertTrue(rr.cleared)
                continue
            bf = brute_force_optimal(p)
            try:
                s = solve(p)
            except InfeasibleError:
                self.assertIsNone(bf)
                continue
            self.assertIsNotNone(bf)
            self.assertEqual(s.num_beats, bf[0])
            self.assertEqual(s.total_changes, bf[1])
            self.assertEqual(s.matchings(), list(bf[2]))
            rr = replay(p, s.matchings())
            self.assertTrue(rr.cleared)
            checked += 1
        self.assertGreater(checked, 5)

    def test_fuzz_replan_combined_prefix_suffix_clears(self):
        rng = random.Random(777)
        from switch_scheduler import replan

        for _ in range(15):
            p = _random_problem(rng, 2, 2)
            try:
                full = solve(p)
            except InfeasibleError:
                continue
            if full.num_beats == 0:
                continue
            k = rng.randint(1, full.num_beats)
            # 用真实回放结果取截至 k 拍的完成矩阵，再随机扰动为合法的更少完成
            rr = replay(p, full.matchings()[:k])
            completion = [list(row) for row in rr.completed]
            for i in range(p.n_in):
                for j in range(p.n_out):
                    if completion[i][j] > 0 and rng.random() < 0.5:
                        completion[i][j] -= 1
            try:
                frozen, suffix = replan(
                    p, full.matchings()[:k], actual_completion=completion
                )
            except InfeasibleError:
                # 收紧的真实完成可能使原窗口无法容纳，属真实不可行
                continue
            combined = [r.matching for r in frozen.records] + [
                r.matching for r in suffix.records
            ]
            rr2 = replay(p, combined)
            self.assertTrue(rr2.cleared)


if __name__ == "__main__":
    unittest.main()
