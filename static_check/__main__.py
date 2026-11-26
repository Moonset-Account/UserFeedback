"""Command line entry point: ``python3 -m static_check graph.json ...``."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .api import audit_flow


def _csv_or_json(value: str | None) -> Any:
    if value is None:
        return None
    text = value.strip()
    if text.startswith("{") or text.startswith("["):
        return json.loads(text)
    return [item.strip() for item in text.split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m static_check",
        description="Check disclosable error codes and diagnostics in an error-flow graph.",
    )
    parser.add_argument("graph", help="Path to a JSON graph or inline JSON document")
    parser.add_argument("--caller", "--identity", dest="caller", default=None)
    parser.add_argument(
        "--public",
        "--diagnostics",
        dest="public",
        help="Comma-separated diagnostic IDs or a JSON grant mapping/list",
    )
    parser.add_argument(
        "--codes",
        "--public-codes",
        dest="codes",
        help="Comma-separated public error codes or a JSON grant mapping/list",
    )
    parser.add_argument(
        "--fields",
        "--allowed-fields",
        dest="fields",
        help="Comma-separated caller-licensed field names",
    )
    parser.add_argument("--json", action="store_true", help="Print the full JSON report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = audit_flow(
        args.graph,
        _csv_or_json(args.public),
        args.caller,
        public_codes=_csv_or_json(args.codes),
        allowed_fields=_csv_or_json(args.fields),
    )

    if args.json:
        json.dump(report.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"caller={report.caller!r} ok={report.ok}")
        for exit_report in report.exits:
            status = "reachable" if exit_report.reachable else "unreachable"
            print(
                f"exit {exit_report.exit_id}: {status}, ok={exit_report.ok}, "
                f"allowed={list(exit_report.allowed_diagnostics)}, "
                f"blocked={list(exit_report.blocked_diagnostics)}"
            )
            for response in exit_report.safe_responses:
                print(f"  safe_response={json.dumps(response, ensure_ascii=False)}")
        for finding in report.all_findings:
            print(f"[{finding.severity}] {finding.code}: {finding.message}")

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
