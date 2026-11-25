"""Command-line interface for checking a JSON circuit document."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .checker import SecurityReport, check_information_flow
from .errors import InfoFlowError
from .policy import Policy

_POLICY_KEYS = (
    "public_inputs",
    "secret_inputs",
    "summary_outputs",
    "external_outputs",
)


def check_document(
    document: dict[str, Any],
    *,
    constant_propagation: bool | None = None,
) -> SecurityReport:
    """Check either a ``{"circuit": ..., "policy": ...}`` document or a circuit.

    Policy labels may alternatively be placed at the top level alongside the
    circuit keys for compact examples.
    """

    if not isinstance(document, dict):
        raise TypeError("top-level JSON value must be an object")

    if "circuit" in document:
        circuit = document["circuit"]
        policy_data = document.get("policy", {}) or {}
    else:
        circuit = document
        policy_data = document.get("policy", {}) or {}

    if not isinstance(policy_data, Policy):
        merged = {key: value for key, value in document.items() if key in _POLICY_KEYS}
        merged.update(policy_data)
        policy = Policy.from_mapping(merged)
    else:
        policy = policy_data

    propagate = (
        constant_propagation
        if constant_propagation is not None
        else bool(document.get("constant_propagation", False))
    )
    return check_information_flow(
        circuit,
        policy,
        constant_propagation=propagate,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m info_flow_checker",
        description="Check Boolean circuits for secret leakage modulo approved summaries.",
    )
    parser.add_argument("path", help="JSON circuit/policy document path")
    parser.add_argument(
        "--propagate-constants",
        action="store_true",
        help="run the semantics-preserving constant propagation pass before checking",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    if argv is not None:
        argv = [str(argument) for argument in argv]
    args = parser.parse_args(argv)
    path = Path(args.path)

    try:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
        report = check_document(
            document,
            constant_propagation=args.propagate_constants,
        )
    except (OSError, json.JSONDecodeError, InfoFlowError, TypeError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    return 0 if report.secure else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
