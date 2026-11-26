"""Command-line demo: print an audit report as deterministic JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .io import audit_from_file
from .sample import build_sample_session


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile logical hardware identities with enumerated devices."
    )
    parser.add_argument(
        "request",
        nargs="?",
        type=Path,
        help="optional JSON audit request; omitted means use the built-in bridge sample",
    )
    args = parser.parse_args(argv)

    if args.request is None:
        report = build_sample_session().report
    else:
        report = audit_from_file(args.request)

    json.dump(report.to_dict(), sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report.feasible else 2


if __name__ == "__main__":
    raise SystemExit(main())
