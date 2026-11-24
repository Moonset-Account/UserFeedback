"""Command-line interface: ``python -m relanalysis <problem.json>``.

The problem document looks like::

    {
      "schema": ["A", "B", "C", "D"],
      "subtables": [
        {"name": "R1", "columns": ["A", "B"]},
        {"name": "R2", "columns": ["B", "C"]},
        {"name": "R3", "columns": ["C", "D"]}
      ],
      "dependencies": [
        {"kind": "MVD", "left": ["A"], "right": ["B"]},
        {"kind": "FD", "left": ["C"], "right": ["D"]}
      ]
    }

Commands::

    python -m relanalysis problem.json              # analyse, print derivation
    python -m relanalysis problem.json --json       # print evidence JSON
    python -m relanalysis --verify evidence.json    # independent verification
"""

from __future__ import annotations

import argparse
import json
import sys

from .analysis import analyze_problem
from .evidence import verify_evidence


def _read_json(path: str) -> object:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise SystemExit(f"cannot read {path!r}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path!r} is not valid JSON: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m relanalysis",
        description="Decide whether a decomposition joins losslessly, with evidence.",
    )
    parser.add_argument("problem", nargs="?", help="problem JSON file")
    parser.add_argument("--json", action="store_true", help="emit the evidence document")
    parser.add_argument(
        "--verify", metavar="EVIDENCE", help="independently verify an evidence JSON file"
    )
    args = parser.parse_args(argv)

    if args.verify:
        document = _read_json(args.verify)
        result = verify_evidence(document)
        print("ACCEPTED" if result.accepted else "REJECTED")
        for note in result.notes:
            print("note:", note)
        for error in result.errors:
            print("error:", error)
        return 0 if result.accepted else 1

    if not args.problem:
        parser.error("a problem file is required unless --verify is used")
    problem = _read_json(args.problem)
    if not isinstance(problem, dict):
        raise SystemExit("problem document must be a JSON object")
    analysis = analyze_problem(problem)
    if args.json:
        json.dump(analysis.to_dict(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(analysis.explain())
        check = verify_evidence(analysis.to_dict())
        print()
        print("independent verification:", "ACCEPTED" if check.accepted else "REJECTED")
        for error in check.errors:
            print("  error:", error)
        return 0 if check.accepted else 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
