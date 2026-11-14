#!/usr/bin/env python3
"""CLI entry point for the SEC-VULN-043 in-memory regression suite.

Runs all deterministic scenarios and prints a JSON report. For every
decision the report lists the approval records used and the rules
triggered. Exit code 0 means every scenario matched its expected
outcome (including the invariant that rejections produce no export
record); exit code 1 signals a regression.

Usage:
    /usr/local/bin/python3 app.py
"""

import json
import sys

from scenarios import run_all


def main() -> int:
    results = run_all()
    report = {
        "suite": "SEC-VULN-043 approval object binding regression",
        "clock_now": 1000,
        "cases": [r.to_dict() for r in results],
        "all_ok": all(r.ok for r in results),
    }
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if report["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
