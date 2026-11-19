"""Command line entry point: ``python -m approval_compat.check routes.json``.

Reads one JSON document ``{"routes": [...]}`` (or a single route object), runs
every route, prints the text diagnosis, and exits non-zero when any route
blocks the release.  ``--json`` prints the machine-readable report instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Mapping, Sequence

from .engine import RouteReport, check_route, summarize
from .model import SchemaError, route_from_dict


def _load_routes(document: object) -> List[Mapping]:
    if isinstance(document, Mapping) and "routes" in document:
        routes = document["routes"]
    elif isinstance(document, list):
        routes = document
    else:
        routes = [document]
    if not isinstance(routes, list) or not routes:
        raise SchemaError("input must contain at least one route")
    for route in routes:
        if not isinstance(route, Mapping):
            raise SchemaError("each route must be a JSON object")
    return routes


def run(path: Path) -> Sequence[RouteReport]:
    with path.open("r", encoding="utf-8") as handle:
        document = json.load(handle)
    return tuple(check_route(route_from_dict(raw)) for raw in _load_routes(document))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m approval_compat.check",
        description=(
            "Check fixed forwarding routes for legacy senders gaining an "
            "approval meaning through ordinary fields."
        ),
    )
    parser.add_argument("routes", type=Path, help="JSON file with one or more routes")
    parser.add_argument(
        "--json", action="store_true", help="emit the machine-readable report"
    )
    args = parser.parse_args(argv)

    try:
        reports = run(args.routes)
    except (OSError, json.JSONDecodeError, SchemaError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        payload = {
            "summary": summarize(reports),
            "reports": [r.as_dict() for r in reports],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for report in reports:
            print(report.format_text())
            print()

    return 0 if all(r.ok for r in reports) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
