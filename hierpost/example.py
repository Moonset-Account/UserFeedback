"""Verification input: total 200, negative noisy readings, a missing branch.

Hierarchy (7 nodes, 4 leaves)::

    root (200)
    ├── urban
    │   ├── urban_east
    │   └── urban_west
    └── rural            <-- entire subtree unmeasured ("缺测" branch)
        ├── rural_north
        └── rural_south

Noisy readings intentionally include negatives (urban_east -3, urban_west -7,
urban 195, root 205).  ``rural`` and both of its leaves carry no measurement,
so the solver must still hand them people while keeping every level additive and
non-negative; the unchanged positive root total constrains the result.

Run ``python3 -m hierpost.example`` (from the repo root) to print the table and
the verifiable totals.
"""

from __future__ import annotations

SPEC = {
    "root_total": 200,
    "nodes": {
        "root": {"parent": None, "weight": 1, "noisy": 205},
        "urban": {"parent": "root", "weight": 3, "noisy": 195},
        "urban_east": {
            "parent": "urban",
            "weight": 2,
            "noisy": -3,
            "leaf_index": 0,
        },
        "urban_west": {
            "parent": "urban",
            "weight": 2,
            "noisy": -7,
            "leaf_index": 1,
        },
        # Unmeasured branch: no "noisy" anywhere below "rural".
        "rural": {"parent": "root"},
        "rural_north": {"parent": "rural", "leaf_index": 2},
        "rural_south": {"parent": "rural", "leaf_index": 3},
    },
}


def main() -> None:
    from .solver import solve_spec

    result = solve_spec(SPEC)
    print("published table")
    for node_id in ("root", "urban", "urban_east", "urban_west", "rural",
                    "rural_north", "rural_south"):
        node = SPEC["nodes"][node_id]
        noisy = node.get("noisy", "missing")
        weight = node.get("weight", "-")
        print(
            f"  {node_id:<12} count={result.counts[node_id]:>4}  "
            f"noisy={str(noisy):>8}  weight={str(weight):>2}  "
            f"cost={result.measured_cost.get(node_id, 0):>6}"
        )
    print()
    print(f"total weighted squared error : {result.total_cost}")
    print(f"leaf vector (leaf_index order): {result.leaf_vector}")
    print(f"level totals (depth -> sum)  : {result.level_totals}")
    print()
    for node_id in ("root", "urban", "rural"):
        children = [
            c
            for c in SPEC["nodes"]
            if SPEC["nodes"][c]["parent"] == node_id
        ]
        child_sum = sum(result.counts[c] for c in children)
        print(
            f"check {node_id}: {result.counts[node_id]} == sum"
            f"{tuple(children)} = {child_sum} -> "
            f"{'OK' if result.counts[node_id] == child_sum else 'FAIL'}"
        )


if __name__ == "__main__":  # pragma: no cover
    main()
