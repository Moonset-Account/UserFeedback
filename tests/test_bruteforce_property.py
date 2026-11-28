"""随机合成数据上与暴力枚举对照，覆盖正常、边界与交错约束。

暴力枚举只使用题面原始定义（逐偏移组合直接核对全部约束），
不共用求解器的任何图算法代码，因此能独立验证：
  1. 求解器判定可行/不可行与枚举一致；
  2. 最小最大偏移改动 D 与枚举最优一致；
  3. 字典序最小偏移向量与枚举最优一致；
  4. 求解结果确实满足全部偏移范围与事件间隔约束。
"""

import itertools
import random
import unittest

import _bootstrap  # noqa: F401

from datemapper import DateMapper, GapConstraint, InfeasibleError


def _brute_force(persons, bounds, proposed, dates, constraints):
    """返回 (状态, 最优偏移字典)；状态为 "feasible" 或 "infeasible"。"""

    domains = [range(bounds[p][0], bounds[p][1] + 1) for p in persons]
    best = None  # (D, 向量元组)
    for combo in itertools.product(*domains):
        offsets = dict(zip(persons, combo))
        feasible = True
        for constraint in constraints:
            pa, ea = constraint.a_event
            pb, eb = constraint.b_event
            gap = (
                dates[pb][eb] + offsets[pb] - (dates[pa][ea] + offsets[pa])
            )
            if constraint.min_gap_days is not None and gap < constraint.min_gap_days:
                feasible = False
                break
            if constraint.max_gap_days is not None and gap > constraint.max_gap_days:
                feasible = False
                break
        if not feasible:
            continue
        d_value = max(abs(offsets[p] - proposed[p]) for p in persons)
        key = (d_value, combo)
        if best is None or key < best:
            best = key
    if best is None:
        return "infeasible", None
    return "feasible", dict(zip(persons, best[1]))


class BruteForceComparisonTests(unittest.TestCase):
    def test_random_instances_match_brute_force(self):
        rng = random.Random(20261128)
        feasible_count = 0
        infeasible_count = 0

        for instance_id in range(60):
            person_count = rng.randint(1, 3)
            persons = list(range(person_count))

            # 窄偏移范围，保证暴力枚举可控（每维至多 7 个取值）。
            bounds = {}
            proposed = {}
            for p in persons:
                low = rng.randint(-3, 1)
                high = rng.randint(low, low + rng.randint(0, 5))
                bounds[p] = (low, high)
                # 拟用偏移有时故意落在范围外，考察边界处理。
                proposed[p] = rng.randint(low - 2, high + 2)

            dates = {p: {} for p in persons}
            event_counter = 0
            for p in persons:
                for _ in range(rng.randint(1, 2)):
                    dates[p][f"e{event_counter}"] = rng.randint(0, 40)
                    event_counter += 1

            constraints = []
            all_refs = [(p, e) for p in persons for e in dates[p]]
            for _ in range(rng.randint(0, 4)):
                if len(all_refs) < 2:
                    break
                a_ref, b_ref = rng.sample(all_refs, 2)
                min_gap = rng.choice([None, rng.randint(-8, 8)])
                max_gap = rng.choice([None, rng.randint(-8, 8)])
                if min_gap is None and max_gap is None:
                    min_gap = rng.randint(-8, 8)
                if (
                    min_gap is not None
                    and max_gap is not None
                    and min_gap > max_gap
                ):
                    min_gap, max_gap = max_gap, min_gap
                constraints.append(
                    GapConstraint(a_ref, b_ref, min_gap, max_gap)
                )

            status, expected_offsets = _brute_force(
                persons, bounds, proposed, dates, constraints
            )

            mapper = DateMapper(
                event_dates=dates,
                offset_bounds=bounds,
                proposed_offsets=proposed,
                gap_constraints=constraints,
            )

            if status == "infeasible":
                infeasible_count += 1
                with self.assertRaises(InfeasibleError) as caught:
                    mapper.finalize()
                # 每个不可行实例的证书都必须能独立验证。
                self.assertTrue(
                    caught.exception.certificate.verify(),
                    msg=f"实例 {instance_id} 证书复核失败",
                )
                continue

            feasible_count += 1
            mapper.finalize()
            self.assertEqual(
                mapper.offsets,
                expected_offsets,
                msg=f"实例 {instance_id} 偏移向量与暴力最优不一致",
            )
            expected_d = max(
                abs(expected_offsets[p] - proposed[p]) for p in persons
            )
            self.assertEqual(mapper.max_change, expected_d)

            # 直接核对发布数据满足所有输入约束与偏移范围。
            for p in persons:
                self.assertGreaterEqual(mapper.offsets[p], bounds[p][0])
                self.assertLessEqual(mapper.offsets[p], bounds[p][1])
            for constraint in constraints:
                pa, ea = constraint.a_event
                pb, eb = constraint.b_event
                gap = (
                    mapper.map_event_ordinal(pb, eb)
                    - mapper.map_event_ordinal(pa, ea)
                )
                if constraint.min_gap_days is not None:
                    self.assertGreaterEqual(gap, constraint.min_gap_days)
                if constraint.max_gap_days is not None:
                    self.assertLessEqual(gap, constraint.max_gap_days)

        # 两类情形都要实际覆盖到，测试才有意义。
        self.assertGreater(feasible_count, 10)
        self.assertGreater(infeasible_count, 5)

    def test_interleaved_cross_and_same_person_constraints(self):
        """交错场景：同人间隔与跨人间隔互相咬合的固定实例。"""

        dates = {0: {"a": 0, "b": 5}, 1: {"c": 3}, 2: {"d": 9}}
        bounds = {0: (-4, 4), 1: (-4, 4), 2: (-4, 4)}
        proposed = {0: 0, 1: 0, 2: 0}
        constraints = [
            GapConstraint((0, "a"), (0, "b"), min_gap_days=5),  # 天然成立
            GapConstraint((0, "b"), (1, "c"), min_gap_days=0),
            GapConstraint((1, "c"), (2, "d"), min_gap_days=0, max_gap_days=10),
            GapConstraint((2, "d"), (0, "a"), max_gap_days=20),
        ]
        status, expected = _brute_force(
            [0, 1, 2], bounds, proposed, dates, constraints
        )
        self.assertEqual(status, "feasible")

        mapper = DateMapper(dates, bounds, proposed, constraints)
        mapper.finalize()
        self.assertEqual(mapper.offsets, expected)

    def test_tight_feasible_boundary_equals_enumeration(self):
        """边界场景：唯一可行向量贴着偏移上下界。"""

        dates = {0: {"a": 0}, 1: {"b": 0}}
        bounds = {0: (-2, 2), 1: (-2, 2)}
        proposed = {0: 0, 1: 0}
        constraints = [GapConstraint((0, "a"), (1, "b"), min_gap_days=4)]
        status, expected = _brute_force(
            [0, 1], bounds, proposed, dates, constraints
        )
        self.assertEqual(status, "feasible")
        mapper = DateMapper(dates, bounds, proposed, constraints)
        mapper.finalize()
        self.assertEqual(mapper.offsets, expected)
        # 枚举确认唯一可行组合是 x0=-2, x1=2。
        self.assertEqual(expected, {0: -2, 1: 2})
        self.assertEqual(mapper.max_change, 2)


if __name__ == "__main__":
    unittest.main()
