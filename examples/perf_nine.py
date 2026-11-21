"""Performance check at the maximum of nine changed nodes."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flowupdate import solve, verify
from flowupdate.model import EGRESS

nodes = [f"n{i}" for i in range(1, 10)]
old = {nodes[i]: (nodes[i + 1] if i + 1 < len(nodes) else EGRESS)
       for i in range(len(nodes))}
# Reversal: n1->OUT, n2->n1, ... -- all nine change, needs nine rounds.
new = {nodes[i]: (EGRESS if i == 0 else nodes[i - 1]) for i in range(len(nodes))}

start = time.perf_counter()
schedule = solve(old, new)
elapsed = time.perf_counter() - start
result = verify(old, new, schedule)
print(f"nine-node worst case: {schedule.round_count} rounds in {elapsed:.3f}s")
print(result.describe())
