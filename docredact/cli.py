"""Command line entry point: redact one instance and re-extract text."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .errors import DocError
from .model import Document, extract_page_speech, extract_page_text


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m docredact",
        description="Localised confidential redaction for shared document objects.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    redact = sub.add_parser("redact", help="remove a character range from one instance")
    redact.add_argument("--input", required=True, help="input document JSON")
    redact.add_argument("--output", required=True, help="output document JSON")
    redact.add_argument("--page", required=True, help="page name holding the instance")
    redact.add_argument(
        "--path",
        required=True,
        action="append",
        help="reference path; repeat the flag per step, or pass a dotted string",
    )
    redact.add_argument("--start", required=True, type=int, help="first code point to remove")
    redact.add_argument("--end", required=True, type=int, help="code point just past the removed range")
    redact.add_argument(
        "--include-private",
        action="store_true",
        help="serialize private pages too (default: public pages only)",
    )
    redact.add_argument(
        "--report",
        action="store_true",
        help="print removed text and speech segments as JSON on stdout",
    )

    extract = sub.add_parser("extract", help="re-extract text or speech from a document")
    extract.add_argument("--input", required=True, help="document JSON (e.g. a redacted export)")
    extract.add_argument("--page", required=True, help="page name to read")
    extract.add_argument("--what", choices=("text", "speech"), default="text")
    return parser


def _path_steps(raw: Sequence[str]) -> tuple[str, ...]:
    if len(raw) == 1:
        return tuple(raw[0].split("."))
    return tuple(raw)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        document = Document.load(args.input)
        if args.command == "redact":
            result = document.redact(args.page, _path_steps(args.path), args.start, args.end)
            exported = result.document.export(public_only=not args.include_private)
            with open(args.output, "w", encoding="utf-8") as handle:
                json.dump(exported, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            if args.report:
                json.dump(
                    {
                        "removed_text": result.removed_text,
                        "removed_speech": list(result.removed_speech),
                        "page": result.page,
                        "path": list(result.path),
                    },
                    sys.stdout,
                    ensure_ascii=False,
                )
                sys.stdout.write("\n")
            return 0

        with open(args.input, "r", encoding="utf-8") as handle:
            exported = json.load(handle)
        if args.what == "text":
            json.dump(extract_page_text(exported, args.page), sys.stdout, ensure_ascii=False)
        else:
            json.dump(extract_page_speech(exported, args.page), sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    except DocError as exc:
        print(f"docredact: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
