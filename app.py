#!/usr/bin/env python3
"""CLI entry point for the offline scanner paired comparator.

Usage:
    python3 app.py --cases cases.json --annotations annotations.json \
        --v1 results_v1.json --v2 results_v2.json [--out report.json]

All inputs are synthetic offline JSON files; nothing is fetched from or
sent to any external system.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from comparator import compare


def _load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Offline paired comparator for two scanner versions.")
    parser.add_argument("--cases", required=True,
                        help="JSON file with the synthetic case list.")
    parser.add_argument("--annotations", required=True,
                        help="JSON file with reference annotations per case.")
    parser.add_argument("--v1", required=True,
                        help="JSON file with scanner v1 results.")
    parser.add_argument("--v2", required=True,
                        help="JSON file with scanner v2 results.")
    parser.add_argument("--out", default=None,
                        help="Write the JSON report here instead of stdout.")
    args = parser.parse_args(argv)

    cases = _load(args.cases)["cases"]
    annotations = _load(args.annotations)["annotations"]
    results_v1 = _load(args.v1)["results"]
    results_v2 = _load(args.v2)["results"]

    report = compare(cases, annotations, results_v1, results_v2)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
