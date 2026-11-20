"""Command-line inspection entry point (read-only).

Usage::

    python -m mime_locator inspect message.eml
    python -m mime_locator locate message.eml --leaf 0 --start 3 --end 7
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from .archive import MimeArchive
from .model import CorruptedPartError, InvalidSelectionError, PartNode


def _print_tree(node: PartNode, depth: int) -> None:
    indent = "  " * depth
    extra = ""
    if node.is_leaf:
        status = "CORRUPTED" if node.is_corrupted else "ok"
        extra = (
            f" leaf={node.leaf_index} encoding={node.encoding} "
            f"decoded={node.decoded_length}B status={status}"
        )
        if node.is_corrupted:
            extra += f" reason={node.corruption_reason}"
    name = f' filename="{node.filename}"' if node.filename else ""
    print(f"{indent}{node.kind}: {node.content_type}{name}{extra}")
    print(f"{indent}  path: {node.path_string()}")
    for child in node.children:
        _print_tree(child, depth + 1)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mime_locator",
        description="Read-only MIME evidence locator",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_parser = sub.add_parser("inspect", help="print the MIME part tree")
    inspect_parser.add_argument("message", help="path to a raw MIME message")

    locate_parser = sub.add_parser(
        "locate", help="map a decoded-byte selection to raw ranges"
    )
    locate_parser.add_argument("message", help="path to a raw MIME message")
    locate_parser.add_argument("--leaf", type=int, required=True)
    locate_parser.add_argument("--start", type=int, required=True)
    locate_parser.add_argument("--end", type=int)

    args = parser.parse_args(list(argv) if argv is not None else None)

    with open(args.message, "rb") as handle:
        archive = MimeArchive(handle.read())

    if args.command == "inspect":
        _print_tree(archive.root, 0)
        return 0

    try:
        location = archive.locate(args.leaf, args.start, args.end)
    except (InvalidSelectionError, CorruptedPartError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"path: {location.path_string()}")
    print(f"leaf: {location.leaf_index}")
    print(f"content_type: {location.content_type}")
    print(f"filename: {location.filename}")
    print(f"encoding: {location.encoding}")
    print(
        f"requested: [{location.requested_start}, {location.requested_end}) "
        f"covered: [{location.covered_start}, {location.covered_end})"
    )
    for start, end in location.raw_ranges:
        print(f"raw: {start}-{end} ({end - start} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
