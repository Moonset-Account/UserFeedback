"""Randomized optimality cross-check against an independent brute force.

Run manually: ``python3 examples/crosscheck_optimality.py [count]``.
Not part of the unittest suite (which is deterministic and self-contained).
"""

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flowupdate.model import EGRESS, unreachable_nodes
from flowupdate.solver import solve


def random_table(rng, nodes):
    while True:
        table = {n: rng.choice(nodes + [EGRESS]) for n in nodes}
        if not unreachable_nodes(table):
            return table


def optimal(old, new, max_round_size):
    changed = sorted(n for n in old if old[n] != new[n])
    n = len(changed)
    states = {}
    for mask in range(1 << n):
        table = dict(old)
        for i in range(n):
            if mask & (1 << i):
                table[changed[i]] = new[changed[i]]
        states[mask] = not unreachable_nodes(table)

    from functools import lru_cache

    @lru_cache(maxsize=None)
    def best(mask):
        if mask == (1 << n) - 1:
            return 0
        result = None
        remaining = [i for i in range(n) if not mask & (1 << i)]
        for size in range(1, min(max_round_size, len(remaining)) + 1):
            import itertools

            for combo in itertools.combinations(remaining, size):
                ok = True
                for sub in range(1 << len(combo)):
                    m = mask
                    for j in range(len(combo)):
                        if sub & (1 << j):
                            m |= 1 << combo[j]
                    if not states[m]:
                        ok = False
                        break
                if not ok:
                    continue
                add = 0
                for j in combo:
                    add |= 1 << j
                rest = best(mask | add)
                if rest is None:
                    continue
                if result is None or 1 + rest < result:
                    result = 1 + rest
        return result

    return best(0)


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    rng = random.Random(20261122)
    nodes = ["A", "B", "C", "D", "E"]
    mismatches = 0
    for trial in range(count):
        old = random_table(rng, nodes)
        new = random_table(rng, nodes)
        if old == new:
            continue
        expected = optimal(old, new, 9)
        schedule = solve(old, new)
        if schedule.round_count != expected:
            mismatches += 1
            print("MISMATCH", old, new, schedule.rounds, "expected", expected)
    print(f"checked {count} random 5-node pairs, mismatches={mismatches}")


if __name__ == "__main__":
    main()
