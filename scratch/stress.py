"""大规模随机压测：更多实例与种子，统计可行/不可行覆盖，全部对照暴力枚举。"""

import itertools
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datemapper import DateMapper, GapConstraint, InfeasibleError


def brute(persons, bounds, proposed, dates, constraints):
    domains = [range(bounds[p][0], bounds[p][1] + 1) for p in persons]
    best = None
    for combo in itertools.product(*domains):
        offsets = dict(zip(persons, combo))
        ok = True
        for c in constraints:
            pa, ea = c.a_event
            pb, eb = c.b_event
            gap = dates[pb][eb] + offsets[pb] - (dates[pa][ea] + offsets[pa])
            if c.min_gap_days is not None and gap < c.min_gap_days:
                ok = False
                break
            if c.max_gap_days is not None and gap > c.max_gap_days:
                ok = False
                break
        if not ok:
            continue
        d_value = max(abs(offsets[p] - proposed[p]) for p in persons)
        key = (d_value, combo)
        if best is None or key < best:
            best = key
    return None if best is None else (best[0], dict(zip(persons, best[1])))


def main():
    feasible = infeasible = 0
    for seed in range(5):
        rng = random.Random(1000 + seed)
        for instance_id in range(40):
            person_count = rng.randint(1, 4)
            persons = list(range(person_count))
            bounds, proposed, dates = {}, {}, {p: {} for p in persons}
            counter = 0
            for p in persons:
                low = rng.randint(-4, 1)
                high = rng.randint(low, low + rng.randint(0, 4))
                bounds[p] = (low, high)
                proposed[p] = rng.randint(low - 2, high + 2)
                for _ in range(rng.randint(1, 2)):
                    dates[p][f"e{counter}"] = rng.randint(-20, 60)
                    counter += 1
            refs = [(p, e) for p in persons for e in dates[p]]
            constraints = []
            for _ in range(rng.randint(0, 5)):
                if len(refs) < 2:
                    break
                a_ref, b_ref = rng.sample(refs, 2)
                lo = rng.choice([None, rng.randint(-10, 10)])
                hi = rng.choice([None, rng.randint(-10, 10)])
                if lo is None and hi is None:
                    lo = rng.randint(-10, 10)
                if lo is not None and hi is not None and lo > hi:
                    lo, hi = hi, lo
                constraints.append(GapConstraint(a_ref, b_ref, lo, hi))

            expected = brute(persons, bounds, proposed, dates, constraints)
            mapper = DateMapper(dates, bounds, proposed, constraints)
            if expected is None:
                infeasible += 1
                try:
                    mapper.finalize()
                except InfeasibleError as exc:
                    assert exc.certificate.verify(), (seed, instance_id)
                    continue
                raise AssertionError(f"seed {seed} 实例 {instance_id} 漏判不可行")
            feasible += 1
            mapper.finalize()
            assert mapper.offsets == expected[1], (seed, instance_id, mapper.offsets, expected)
            assert mapper.max_change == expected[0]
    print(f"压测完成：可行实例 {feasible} 个，不可行实例 {infeasible} 个，全部与暴力枚举一致")


if __name__ == "__main__":
    main()
