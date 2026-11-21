"""Worst-case timing: nine changed nodes where many multi-node rounds are safe.

Run manually: ``python3 examples/perf_stress.py``.
"""

import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flowupdate import solve, verify
from flowupdate.model import EGRESS, unreachable_nodes

rng = random.Random(7)
nodes = [f"n{i}" for i in range(1, 10)]

worst = 0.0
worst_case = None
for trial in range(40):
    while True:
        old = {n: rng.choice(nodes + [EGRESS]) for n in nodes}
        if not unreachable_nodes(old):
            break
    while True:
        new = {n: rng.choice(nodes + [EGRESS]) for n in nodes}
        if not unreachable_nodes(new):
            break
    changed = [n for n in nodes if old[n] != new[n]]
    if len(changed) < 9:
        continue
    start = time.perf_counter()
    schedule = solve(old, new)
    elapsed = time.perf_counter() - start
    assert verify(old, new, schedule).ok
    if elapsed > worst:
        worst = elapsed
        worst_case = (old, new, schedule.round_count)

print(f"worst observed solve time over 9-node dense trials: {worst:.3f}s")
if worst_case:
    print("case rounds:", worst_case[2])
