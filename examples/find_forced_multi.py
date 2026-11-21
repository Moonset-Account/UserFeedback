"""Search for instances where no single-node-at-a-time ordering is safe.

If such an instance exists, it shows a within-round multi-node update is
*required*, which is exactly the "arbitrary order within a round" hazard the
problem describes.
"""

import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flowupdate.model import EGRESS, unreachable_nodes
from flowupdate.solver import NoScheduleError, solve

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
    found = 0
    for old in tables:
        for new in tables:
            if old == new:
                continue
            try:
                solve(old, new, max_round_size=1)
                continue
            except NoScheduleError:
                pass
            try:
                s = solve(old, new, max_round_size=len(NODES))
            except NoScheduleError:
                continue
            found += 1
            if found <= 8:
                print(f"old={old}")
                print(f"new={new}")
                print(f"  multi-node plan: {s.rounds}")
    print(f"total instances needing a multi-node round: {found}")


if __name__ == "__main__":
    main()
