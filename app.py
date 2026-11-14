#!/usr/bin/env python3
"""CLI entry: static checker for SQL expression trees.

Usage: python3 app.py <rules.json> <tree.json>

Exit codes: 0 = no risks and no binding mismatches;
            1 = risks or mismatches found;
            2 = usage / input error.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from checker import CheckError, analyze


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: python3 app.py <rules.json> <tree.json>\n")
        return 2
    try:
        with open(argv[1], "r", encoding="utf-8") as f:
            rules = json.load(f)
        with open(argv[2], "r", encoding="utf-8") as f:
            tree = json.load(f)
        report = analyze(tree, rules)
    except (OSError, ValueError, CheckError) as exc:
        sys.stderr.write("error: %s\n" % (exc,))
        return 2
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    s = report["summary"]
    return 1 if (s["risk_count"] or s["mismatch_count"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
