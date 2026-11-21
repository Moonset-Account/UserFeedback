import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flowupdate import load_problem, solve, verify

for name in ("swap_pair", "chain_reversal", "nine_node_reversal"):
    path = os.path.join(ROOT, "examples", name + ".json")
    old, new, label = load_problem(path)
    schedule = solve(old, new)
    result = verify(old, new, schedule)
    print(f"== {name} ==")
    print(json.dumps(schedule.as_dict(), indent=2))
    print(result.describe())
    print()
