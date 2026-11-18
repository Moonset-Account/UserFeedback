#!/usr/bin/env python3
"""Local verification entry point for the dissect.regf BigData QA cases.

Runs the real case implementations from ``tests/bigdata_cases.py`` (the same
functions pytest runs) and emits one JSON record per case on stdout. Each
record contains the case id, the number of assertions, pass/fail/skip counts,
the replay seed (where applicable), forensic sample offsets and an
expected/observed summary for every failed assertion.

Exit code is non-zero if any case fails, is unknown, or records zero
assertions -- this wrapper is a runner, not a proof by itself.

Usage:
    tools/local_verify.py --list
    tools/local_verify.py --case C01   (C01/C02/C03/C04; default: run all)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tests.bigdata_cases import CASES, run_case  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="list available cases and exit")
    parser.add_argument("--case", choices=sorted(CASES), help="run a single case (default: run all)")
    args = parser.parse_args(argv)

    if args.list:
        print(
            json.dumps(
                [{"case": case_id, "title": CASES[case_id]["title"]} for case_id in sorted(CASES)],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    case_ids = [args.case] if args.case else sorted(CASES)
    ok = True
    for case_id in case_ids:
        result = run_case(case_id)
        print(json.dumps(result, ensure_ascii=False))
        if result["failed"] or not result["assertions"] or result["skipped"] >= result["assertions"]:
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
