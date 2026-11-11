#!/usr/bin/env python3
"""Arrival-order invariance check for any event log.

Re-runs the verifier on many shuffled copies of the same history and asserts
that the report is byte-identical every time. This is the regression guard
for the requirement: "same history, only the arrival order changed, the
verdict must stay consistent".

Usage:
    python3 -I check_order_invariance.py <event-log.json> [--iterations 50]
"""

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from handle_audit import Verifier, load_events  # noqa: E402
from handle_audit.report import render_json  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log")
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--seed", type=int, default=20261112)
    args = parser.parse_args(argv)

    with open(args.log, encoding="utf-8") as fh:
        records = json.load(fh)

    baseline = render_json(Verifier(load_events(records)).run())
    rng = random.Random(args.seed)
    failures = 0
    for i in range(args.iterations):
        shuffled = records[:]
        rng.shuffle(shuffled)
        observed = render_json(Verifier(load_events(shuffled)).run())
        if observed != baseline:
            failures += 1
            print(f"iteration {i}: report differs under shuffle")
    if failures:
        print(f"FAIL: {failures}/{args.iterations} shuffles changed the report")
        return 1
    print(
        f"OK: {args.iterations} shuffled arrivals produced identical "
        f"reports ({len(records)} events)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
