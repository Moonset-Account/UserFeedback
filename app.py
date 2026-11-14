#!/usr/bin/env python3
"""CLI entry point for the in-process login quota adjudicator.

Subcommands
-----------
decide   Adjudicate a single request JSON document.
batch    Replay a JSON list of requests against one adjudicator.
demo     Run the built-in, injected-clock scenarios (window boundary,
         retry, and two requests racing for the last unit).

Every subcommand prints one JSON object to stdout and nothing else. Exit
codes are documented in README.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from quota import (
    ClockRegression,
    Decision,
    QuotaAdjudicator,
    QuotaConfig,
    RequestIdConflict,
    ValidationError,
)

EXIT_OK = 0
EXIT_INPUT_ERROR = 1
EXIT_DENIED = 2
EXIT_CONFLICT = 4
EXIT_CLOCK_OFF = 5


# -- I/O helpers ------------------------------------------------------------


def _reject_double_stdin(*paths: str | None) -> None:
    """At most one input may consume stdin; two '-' paths would silently truncate."""

    if sum(1 for path in paths if path in (None, "-")) > 1:
        raise ValidationError("at most one input path may be '-' (stdin)")


def _load_json(path: str | None) -> Any:
    if path is None or path == "-":
        text = sys.stdin.read()
        source = "<stdin>"
    else:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        source = path
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{source}: invalid JSON: {exc}") from exc


def _dump(payload: Any) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")


def _build_config(document: Any) -> QuotaConfig:
    return QuotaConfig.from_mapping(document)


def _require_request_fields(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValidationError("request must be a JSON object")
    required = {"tenant", "account", "source", "request_id", "now"}
    missing = required - set(document)
    unknown = set(document) - required
    if missing:
        raise ValidationError(f"request is missing keys: {sorted(missing)}")
    if unknown:
        raise ValidationError(f"request has unknown keys: {sorted(unknown)}")
    return document


def _adjudicate(adjudicator: QuotaAdjudicator, request: dict[str, Any]) -> Decision:
    return adjudicator.adjudicate(
        tenant=request["tenant"],
        account=request["account"],
        source=request["source"],
        request_id=request["request_id"],
        now=request["now"],
    )


def _error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, (RequestIdConflict, ClockRegression)):
        return exc.to_dict()
    return {"error": "invalid_input", "message": str(exc)}


def _exit_code_for(exc: Exception) -> int:
    if isinstance(exc, RequestIdConflict):
        return EXIT_CONFLICT
    if isinstance(exc, ClockRegression):
        return EXIT_CLOCK_OFF
    return EXIT_INPUT_ERROR


# -- subcommands ------------------------------------------------------------


def cmd_decide(args: argparse.Namespace) -> int:
    _reject_double_stdin(args.config, args.request)
    config = _build_config(_load_json(args.config))
    request = _require_request_fields(_load_json(args.request))
    adjudicator = QuotaAdjudicator(config)
    try:
        decision = _adjudicate(adjudicator, request)
    except (ValidationError, RequestIdConflict, ClockRegression) as exc:
        _dump(_error_payload(exc))
        return _exit_code_for(exc)
    _dump(decision.to_dict())
    return EXIT_OK if decision.allowed else EXIT_DENIED


def cmd_batch(args: argparse.Namespace) -> int:
    _reject_double_stdin(args.config, args.requests)
    config = _build_config(_load_json(args.config))
    document = _load_json(args.requests)
    if isinstance(document, dict):
        raw_requests = document.get("requests")
        if raw_requests is None:
            raise ValidationError("batch object must contain a 'requests' list")
    else:
        raw_requests = document
    if not isinstance(raw_requests, list) or not raw_requests:
        raise ValidationError("requests must be a non-empty JSON list")

    adjudicator = QuotaAdjudicator(config)
    results: list[dict[str, Any]] = []
    any_denied = False
    hard_error = False
    error_code = EXIT_OK
    for index, raw in enumerate(raw_requests):
        try:
            request = _require_request_fields(raw)
            decision = _adjudicate(adjudicator, request)
        except (ValidationError, RequestIdConflict, ClockRegression) as exc:
            hard_error = True
            error_code = max(error_code, _exit_code_for(exc))
            payload = _error_payload(exc)
            payload["index"] = index
            results.append(payload)
            continue
        entry = decision.to_dict()
        entry["index"] = index
        results.append(entry)
        any_denied = any_denied or not decision.allowed

    _dump(
        {
            "results": results,
            "allowed": sum(1 for r in results if r.get("decision") == "allow"),
            "denied": sum(1 for r in results if r.get("decision") == "deny"),
            "errors": sum(1 for r in results if "error" in r),
        }
    )
    if hard_error:
        return error_code
    return EXIT_DENIED if any_denied else EXIT_OK


def cmd_demo(args: argparse.Namespace) -> int:
    from quota.demo import run_scenarios

    scenarios = run_scenarios()
    failures = [name for name, body in scenarios.items() if not body["passed"]]
    _dump({"scenarios": scenarios, "passed": not failures, "failed_scenarios": failures})
    return EXIT_OK if not failures else EXIT_INPUT_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.py", description="Two-layer (account + source) login quota adjudicator"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    decide = sub.add_parser("decide", help="adjudicate one request document")
    decide.add_argument("--config", required=True, help="quota config JSON path or '-' for stdin")
    decide.add_argument("--request", required=True, help="request JSON path or '-' for stdin")
    decide.set_defaults(handler=cmd_decide)

    batch = sub.add_parser("batch", help="replay a list of requests in order")
    batch.add_argument("--config", required=True, help="quota config JSON path or '-' for stdin")
    batch.add_argument("--requests", required=True, help="request list JSON path or '-'")
    batch.set_defaults(handler=cmd_batch)

    demo = sub.add_parser("demo", help="run the injected-clock demonstration scenarios")
    demo.set_defaults(handler=cmd_demo)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except ValidationError as exc:
        _dump({"error": "invalid_input", "message": str(exc)})
        return EXIT_INPUT_ERROR
    except OSError as exc:
        _dump({"error": "io_error", "message": str(exc)})
        return EXIT_INPUT_ERROR


if __name__ == "__main__":
    sys.exit(main())
