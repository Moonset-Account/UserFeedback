#!/usr/bin/env python3
"""Entry point: build the diagnostics page from JSON.

Usage
-----
    python3 app.py --data samples/errors.json \\
                   --allowlist samples/allowlist.json \\
                   --out build/diagnostics.html

    python3 app.py --data ... --allowlist ... --query E42 --out build/hits.html

    python3 app.py --data ... --allowlist ... --report build/report.json

Exit codes: 0 = success, 2 = bad data/allowlist/arguments, 1 = unexpected error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from diagnostics.link_policy import ConfigError, load_policy
from diagnostics.model import DataError, load_dataset
from diagnostics.render import build_report, render_page

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_BAD_INPUT = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="Render a local device-error diagnostics page from JSON data.",
    )
    parser.add_argument("--data", required=True, help="path to the records JSON file")
    parser.add_argument(
        "--allowlist", required=True, help="path to the documentation-host allowlist JSON"
    )
    parser.add_argument("--out", default="build/diagnostics.html", help="HTML output path")
    parser.add_argument(
        "--report",
        default=None,
        help="optional path to write a JSON report parsed back from the generated DOM",
    )
    parser.add_argument(
        "--query",
        default=None,
        help="optional search text; matches are wrapped in <mark> with characters preserved",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        dataset = load_dataset(args.data)
        policy = load_policy(args.allowlist)
    except (DataError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    markup = render_page(dataset, policy, query=args.query)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markup, encoding="utf-8")

    print(f"wrote {out_path} ({len(markup)} chars, {len(dataset.records)} records)")

    if args.report:
        report = build_report(markup)
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {report_path}")

    return EXIT_OK


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        print(f"unexpected error: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_UNEXPECTED)
