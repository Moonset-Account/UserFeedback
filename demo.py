#!/usr/bin/env python3
"""Live demonstration and self-check for the streaming median window.

Run from the workspace root::

    python3 demo.py            # default size
    python3 demo.py --rows 5000

Nothing here is pre-baked: every record is generated with a fixed RNG seed,
the streaming operator's medians are compared against the brute-force
reference in ``median_window.reference_output``, and only the comparison
outcome is reported.  All data is synthetic and generated in-process.
"""

from __future__ import annotations

import argparse
import random

from median_window import MedianWindow, reference_output


def make_records(rng: random.Random, rows: int, partitions: int) -> list[tuple]:
    records: list[tuple] = []
    for part_index in range(partitions):
        partition = f"device-{part_index:02d}"
        time = rng.randint(-2, 3)
        for _ in range(rows):
            time += rng.choice([0, 0, 1, 1, 1, 2, 3])
            value = rng.choice([None, rng.randint(-10, 10), rng.randint(0, 3)])
            records.append((partition, time, value))
    return records


def split_points(rng: random.Random, length: int, batches: int) -> list[int]:
    return sorted(rng.sample(range(1, length), k=min(batches, length - 1)))


def run_in_batches(records, window: int, cuts, seed: int) -> list:
    op = MedianWindow(window, seed=seed)
    out = []
    start = 0
    for cut in list(cuts) + [len(records)]:
        out.extend(r.median for r in op.push_batches([records[start:cut]]))
        start = cut
    op.finish()
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=2000)
    parser.add_argument("--window", type=int, default=4)
    parser.add_argument("--partitions", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20261122)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    records = make_records(rng, args.rows, args.partitions)
    window = args.window

    single = MedianWindow(window, seed=1, retain_results=True)
    single.push_many(records)
    single.finish()
    streamed = [row.median for row in single.results]

    expected = reference_output(records, window)
    print(f"records          : {len(records)}")
    print(f"partitions       : {args.partitions}")
    print(f"window W         : {window}")
    print(f"streaming == ref : {streamed == expected}")

    non_null = sum(1 for m in expected if m is not None)
    print(f"non-NULL outputs : {non_null} / {len(expected)}")
    print(f"first medians    : {streamed[:12]}")

    stats = single.stats()
    print(
        "churn            : "
        f"insertions={stats.insertions} deletions={stats.deletions} "
        f"rotations={stats.rotations} probes={stats.probes} "
        f"rebuilds={stats.rebuilds}"
    )
    print(
        "residency        : "
        f"max_resident={stats.max_resident} max_window_rows={stats.max_window_rows}"
    )

    # Batch-split invariance: random splits, including inside equal-time runs.
    ok = True
    for trial in range(20):
        cuts = split_points(rng, len(records), rng.randint(1, 50))
        got = run_in_batches(records, window, cuts, seed=trial)
        if got != streamed:
            ok = False
            print(f"batch split {trial} MISMATCH at cuts {cuts[:5]}...")
            break
    print(f"batch splits agree: {ok}")

    # A cut placed exactly inside the largest equal-time group.
    group_start = 0
    best = (0, 0)
    while group_start < len(records):
        end = group_start
        while (
            end + 1 < len(records)
            and records[end + 1][0] == records[group_start][0]
            and records[end + 1][1] == records[group_start][1]
        ):
            end += 1
        if end - group_start > best[1] - best[0]:
            best = (group_start, end)
        group_start = end + 1
    if best[1] > best[0] + 1:
        cut = (best[0] + best[1]) // 2 + 1
        inside = run_in_batches(records, window, [cut], seed=99)
        print(f"split inside equal-time run at {cut}: {inside == streamed}")
    else:
        print("split inside equal-time run: no run long enough in this sample")

    return 0 if ok and streamed == expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
