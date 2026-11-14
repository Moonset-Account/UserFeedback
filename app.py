#!/usr/bin/env python3
"""CLI entry: offline audit of ordered image layer archives for residual
credential markers.

Usage:
    python3 app.py --layers L0.tar L1.tar ... --rules rules.json [--output R.json]
                   [--fail-on never|visible|any]

Layers are given in apply order (oldest first). The scan is read-only; the
report contains only rule ids, layer indexes, paths and redacted fingerprints.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auditor


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Offline audit of ordered image layer archives for "
                    "residual credential markers (synthetic test markers only).")
    parser.add_argument("--layers", nargs="+", required=True,
                        help="Layer archive paths in apply order (oldest first).")
    parser.add_argument("--rules", required=True,
                        help="JSON file: list of {id, pattern} marker rules.")
    parser.add_argument("--output",
                        help="Write report JSON here (default: stdout).")
    parser.add_argument("--fail-on", choices=["never", "visible", "any"],
                        default="never",
                        help="Exit 1 when matches are visible in the final view "
                             "('visible') or exist at all ('any'). Default: never.")
    args = parser.parse_args(argv)

    try:
        rules = auditor.load_rules(args.rules)
    except (OSError, ValueError) as exc:
        print("error: cannot load rules: %s" % exc, file=sys.stderr)
        return 2
    for lp in args.layers:
        if not os.path.isfile(lp):
            print("error: layer not found: %s" % lp, file=sys.stderr)
            return 2

    result = auditor.audit(args.layers, rules)
    report = auditor.report_to_dict(result)
    text = json.dumps(report, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)

    summary = report["summary"]
    if args.fail_on == "visible" and summary["visible"] > 0:
        return 1
    if args.fail_on == "any" and summary["total_matches"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
