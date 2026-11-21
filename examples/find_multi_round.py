"""Find small instances whose optimal plan uses a multi-node round."""

import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flowupdate.model import EGRESS, unreachable_nodes
from flowupdate.solver import solve

NODES = ["A", "B", "C", "D"]


def valid(table):
    return not unreachable_nodes(table)


def all_tables(nodes):
    for combo in itertools.product(nodes + [EGRESS], repeat=len(nodes)):
        table = dict(zip(nodes, combo))
        if valid(table):
            yield table


def main():
    tables = list(all_tables(NODES))
    best = []
    for old in tables:
        for new in tables:
            if old == new:
                continue
            s = solve(old, new)
            if any(len(r) > 1 for r in s.rounds):
                total_multi = sum(len(r) for r in s.rounds if len(r) > 1)
                best.append((total_multi, s.round_count, old, new, s.rounds))
    best.sort(key=lambda t: (-t[0], t[1]))
    print(f"{len(best)} pairs use a multi-node round; showing top 5")
    for total_multi, rc, old, new, rounds in best[:5]:
        print(f"old={old} new={new} rounds={rounds}")


if __name__ == "__main__":
    main()
