#!/usr/bin/env python3
"""Command line entry point for the build artifact triage pipeline.

Exit codes:
    0  all build batches are publishable
    1  unexpected internal error
    2  fatal input error (unreadable/malformed index, bad CLI arguments)
    3  plans were produced but one or more batches are blocked
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from triage.engine import TriageResult, triage
from triage.errors import TriageError
from triage.models import IndexSpec, load_index

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_FATAL = 2
EXIT_BLOCKED = 3


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False, ensure_ascii=False)
        handle.write("\n")


def _pair_indexes(
    index_values: Sequence[str], root_values: Sequence[str]
) -> list[IndexSpec]:
    if not index_values:
        raise TriageError("at least one --index is required")
    if len(root_values) not in (1, len(index_values)):
        raise TriageError(
            "--root must be given once, or once per --index "
            f"(got {len(index_values)} indexes and {len(root_values)} roots)"
        )
    roots = (
        root_values
        if len(root_values) == len(index_values)
        else root_values * len(index_values)
    )
    specs = []
    for index_value, root_value in zip(index_values, roots):
        index_path = Path(index_value)
        root = Path(root_value)
        if not index_path.is_file():
            raise TriageError(f"index file not found: {index_path}")
        if not root.is_dir():
            raise TriageError(f"artifact root is not a directory: {root}")
        specs.append(
            IndexSpec(index_path=index_path.resolve(), root=root.resolve())
        )
    return specs


def _print_text_summary(result: TriageResult, out) -> None:
    public = result.public_plan["summary"]
    internal = result.internal_plan["summary"]
    manifest = result.block_manifest["summary"]
    lines = [
        "triage summary",
        f"  batches total      : {manifest['total_batches']}",
        f"  batches publishable: {manifest['publishable_batch_count']}",
        f"  batches blocked    : {manifest['blocked_batch_count']}",
        f"  public files       : {public['file_count']} "
        f"({public['batch_count']} batches)",
        f"  symbol entries     : {internal['symbol_entry_count']} "
        f"across {internal['architecture_count']} architecture(s)",
        f"  merged entries     : {internal['merged_entry_count']}",
        f"  symbol conflicts   : {internal['conflict_count']}",
        "",
    ]
    for blocked in result.block_manifest["blocked_batches"]:
        lines.append(
            f"BLOCKED {blocked['build_id']} [{blocked['architecture']}]"
        )
        for reason in blocked["reasons"]:
            lines.append(f"  - {reason['code']}: {reason['message']}")
    if result.block_manifest["blocked_batches"]:
        lines.append("")
    for batch in result.block_manifest["publishable"]["batches"]:
        lines.append(
            f"PUBLISH {batch['build_id']} [{batch['architecture']}] "
            f"public={batch['public_file_count']} "
            f"symbols={batch['symbol_file_count']}"
        )
    out.write("\n".join(lines).rstrip() + "\n")


def _cmd_triage(args: argparse.Namespace) -> int:
    specs = _pair_indexes(args.index, args.root)
    builds = [load_index(spec.index_path, spec.root) for spec in specs]
    result = triage(builds)
    _write_json(Path(args.public_plan), result.public_plan)
    _write_json(Path(args.internal_plan), result.internal_plan)
    _write_json(Path(args.manifest), result.block_manifest)
    if args.json_summary:
        _write_json(
            Path(args.json_summary),
            {
                "public_plan": args.public_plan,
                "internal_plan": args.internal_plan,
                "manifest": args.manifest,
                "summary": result.block_manifest["summary"],
            },
        )
    _print_text_summary(result, sys.stdout)
    return EXIT_BLOCKED if result.blocked_batches else EXIT_OK


def _iter_plan_files(payload: dict):
    archive = payload.get("archive")
    if archive == "public":
        for batch in payload.get("batches", []):
            for entry in batch.get("files", []):
                yield archive, batch["build_id"], batch["architecture"], entry
    elif archive == "internal":
        for section in payload.get("architectures", []):
            for debug_id, entry in section.get("debug_ids", {}).items():
                yield archive, section["architecture"], debug_id, entry
    else:
        raise TriageError("unrecognized plan document (missing 'archive')")


def _cmd_list(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TriageError("plan document must be a JSON object")
    if args.format == "json":
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return EXIT_OK
    archive = payload.get("archive", "?")
    print(f"archive: {archive}")
    for item in _iter_plan_files(payload):
        plan_kind, first, second, entry = item
        if plan_kind == "public":
            debug = entry.get("debug_id") or "-"
            print(
                f"  [{first}/{second}] {entry['path']} "
                f"role={entry['role']} debug_id={debug} "
                f"sha256={entry['sha256'][:12]}"
            )
        else:
            merged = "merged" if entry.get("merged") else "single"
            print(
                f"  [{first}] debug_id={second} {entry['canonical_path']} "
                f"{merged} refs={entry['reference_count']} "
                f"sha256={entry['sha256'][:12]}"
            )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.py",
        description=(
            "Split build artifacts into a public download archive plan and "
            "an internal debug symbol archive plan."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    triage_parser = sub.add_parser(
        "triage",
        help="evaluate build indexes and emit archive plans",
    )
    triage_parser.add_argument(
        "--index",
        action="append",
        required=True,
        help="path to a build JSON index (repeat once per build batch)",
    )
    triage_parser.add_argument(
        "--root",
        action="append",
        required=True,
        help=(
            "artifact root; give one shared root or one root per --index, "
            "in the same order"
        ),
    )
    triage_parser.add_argument(
        "--public-plan",
        required=True,
        help="output path for the public archive plan JSON",
    )
    triage_parser.add_argument(
        "--internal-plan",
        required=True,
        help="output path for the internal symbol archive plan JSON",
    )
    triage_parser.add_argument(
        "--manifest",
        required=True,
        help="output path for the blocked-batch manifest JSON",
    )
    triage_parser.add_argument(
        "--json-summary",
        help="optional output path for a machine-readable run summary",
    )
    triage_parser.set_defaults(func=_cmd_triage)

    list_parser = sub.add_parser(
        "list",
        help="list the contents of a previously generated archive plan",
    )
    list_parser.add_argument("plan", help="path to a public/internal plan JSON")
    list_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="listing format (default: text)",
    )
    list_parser.set_defaults(func=_cmd_list)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except TriageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FATAL
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FATAL


if __name__ == "__main__":
    raise SystemExit(main())
