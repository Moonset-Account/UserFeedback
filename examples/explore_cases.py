"""Exploration: enumerate small valid old/new table pairs, look for hard cases.

Not part of the shipped API or the unittest suite; run manually to inform the
examples and boundary tests.  Run with ``python3 examples/explore_cases.py``.
"""

import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flowupdate.model import EGRESS, unreachable_nodes
from flowupdate.solver import NoScheduleError, solve

NODES = ["A", "B", "C", "D"]
CHOICES = NODES + [EGRESS]  # next hop candidates for each node


def valid(table):
    if any(table[n] == n for n in NODES):
        return False
    return not unreachable_nodes(table)


def all_tables(nodes):
    for combo in itertools.product(nodes + [EGRESS], repeat=len(nodes)):
        table = dict(zip(nodes, combo))
        if valid(table):
            yield table


def main():
    tables = list(all_tables(NODES))
    print(f"{len(tables)} valid tables on {NODES}")
    hard = []
    max_rounds = 0
    for old in tables:
        for new in tables:
            if old == new:
                continue
            try:
                schedule = solve(old, new, max_changed=9)
            except NoScheduleError:
                hard.append((old, new))
                continue
            max_rounds = max(max_rounds, schedule.round_count)
    print(f"unschedulable pairs: {len(hard)}")
    for old, new in hard[:5]:
        print("  old", old, "new", new)
    print(f"max rounds observed: {max_rounds}")


if __name__ == "__main__":
    main()
