#!/usr/bin/env python3
"""Collect a bounded Git diff for the composite Action."""

import argparse
import sys

from src.gitdiff import collect_git_diff
from src.safeio import write_private_atomic


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        payload = collect_git_diff(base=args.base, head=args.head)
        write_private_atomic(args.output, payload, "Action diff output")
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: diff collection failed", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
