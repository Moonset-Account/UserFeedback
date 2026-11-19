"""Command-line entry point: ``python -m revocation_store``.

Subcommands
-----------
ingest   Store one table (or an array of tables) from a JSON file.
lookup   Resolve a serial's status at an instant.
tables   List stored tables (optionally filtered by issuer/scope).
entries  List the entries of one stored table.

All output is JSON on stdout so the CLI is scriptable.  A rejected table
exits with status 1 and an ``error`` object; malformed usage exits with 2.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from .jsonio import tables_from_json, table_to_dict
from .model import (
    ConflictingTableError,
    TableValidationError,
    isoformat_utc,
)
from .store import RevocationStore


def _read_json(path: str):
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise TableValidationError(f"cannot read {path!r}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TableValidationError(f"{path!r} is not valid JSON: {exc}") from exc


def _parse_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TableValidationError(
            f"--at is not a valid ISO-8601 timestamp: {value!r}"
        ) from exc
    return parsed


def _emit(payload) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="revocation_store",
        description="Local certificate-revocation state store (full + delta tables).",
    )
    parser.add_argument(
        "--db",
        default="revocation.sqlite3",
        help="SQLite database path (default: revocation.sqlite3)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="store tables from a JSON file")
    ingest.add_argument("--file", required=True, help="JSON file with one table or an array")

    lookup = subparsers.add_parser("lookup", help="resolve a serial at an instant")
    lookup.add_argument("--issuer", required=True)
    lookup.add_argument("--scope", required=True)
    lookup.add_argument("--serial", required=True)
    lookup.add_argument("--at", required=True, help="ISO-8601 query instant")

    listing = subparsers.add_parser("tables", help="list stored tables")
    listing.add_argument("--issuer")
    listing.add_argument("--scope")

    entries = subparsers.add_parser("entries", help="list entries of one table")
    entries.add_argument("--table-id", required=True, type=int)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        with RevocationStore(args.db) as store:
            if args.command == "ingest":
                tables = tables_from_json(_read_json(args.file))
                results = [store.ingest(table).as_dict() for table in tables]
                _emit({"ingested": results, "count": len(results)})
                return 0

            if args.command == "lookup":
                result = store.lookup(
                    issuer=args.issuer,
                    scope=args.scope,
                    serial=args.serial,
                    at=_parse_at(args.at),
                )
                _emit(result.as_dict())
                return 0

            if args.command == "tables":
                _emit(store.list_tables(issuer=args.issuer, scope=args.scope))
                return 0

            if args.command == "entries":
                rows = store.table_entries(args.table_id)
                _emit({"table_id": args.table_id, "entries": rows})
                return 0
    except ConflictingTableError as exc:
        _emit({"error": "conflict", "message": str(exc)})
        return 1
    except TableValidationError as exc:
        _emit({"error": "invalid_table", "message": str(exc)})
        return 1

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
