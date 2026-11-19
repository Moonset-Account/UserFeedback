"""Command line entry points.

Two subcommands:

* ``preprocess``  raw roster JSON -> preprocessed roster JSON (digests only)
* ``review``      preprocessed roster JSON + outbound shards JSON -> report

All inputs are local JSON files.  Example::

    python -m outbound_review.cli preprocess \
        --roster fixtures/roster_raw.json --out fixtures/roster_digests.json
    python -m outbound_review.cli review \
        --roster fixtures/roster_digests.json \
        --shards fixtures/outbound_shards.json --out report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Sequence

from .digest import TEST_KEY
from .review import run_review
from .roster import InternalIndex, load_preprocessed, preprocess_roster


def _read_json(path: str) -> object:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: str, payload: object) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _preprocess(args: argparse.Namespace) -> int:
    raw = _read_json(args.roster)
    if not isinstance(raw, list):
        raise SystemExit("roster input must be a JSON list of rows")
    entries = preprocess_roster(raw, key=TEST_KEY, code_field=args.code_field)
    payload = {
        "entries": [
            {"row_code": entry.row_code, "digests": entry.digests}
            for entry in entries
        ]
    }
    _write_json(args.out, payload)
    return 0


def _review(args: argparse.Namespace) -> int:
    roster_payload = _read_json(args.roster)
    if not isinstance(roster_payload, dict):
        raise SystemExit("roster input must be a JSON object")
    index: InternalIndex = load_preprocessed(roster_payload)
    shards_payload = _read_json(args.shards)
    if not isinstance(shards_payload, dict) or "shards" not in shards_payload:
        raise SystemExit("shards input must be {'shards': [...]} JSON")
    designated: Optional[List[str]] = args.columns.split(",") if args.columns else None
    if designated is not None:
        designated = [column.strip() for column in designated if column.strip()]
    report = run_review(
        index,
        shards_payload["shards"],
        designated_columns=designated,
        key=TEST_KEY,
    )
    _write_json(args.out, report.to_dict())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="outbound_review",
        description="Review outbound table shards against a preprocessed roster.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pre = sub.add_parser("preprocess", help="digest a raw internal roster")
    pre.add_argument("--roster", required=True, help="raw roster JSON file")
    pre.add_argument("--out", required=True, help="output preprocessed JSON file")
    pre.add_argument(
        "--code-field", default="row_code", help="field carrying the internal code"
    )
    pre.set_defaults(func=_preprocess)

    rev = sub.add_parser("review", help="match outbound shards against a roster")
    rev.add_argument("--roster", required=True, help="preprocessed roster JSON file")
    rev.add_argument("--shards", required=True, help="outbound shards JSON file")
    rev.add_argument("--out", required=True, help="output report JSON file")
    rev.add_argument(
        "--columns",
        default="",
        help="comma-separated designated columns (default: all roster columns)",
    )
    rev.set_defaults(func=_review)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
