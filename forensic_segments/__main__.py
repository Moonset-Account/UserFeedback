"""Command-line entry point: read image metadata JSON, extract a range.

Usage::

    python3 -m forensic_segments metadata.json --start 0 --end 4096
    python3 -m forensic_segments metadata.json --start 0 --end 4096 \
        --prefer pass2.img --json

The metadata file describes synthetic or imported evidence metadata only; the
tool never opens media or touches the network.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from . import Image, ImageLibrary, Segment, SegmentStatus
from .errors import ForensicSegmentsError


def _segment_from_json(raw: Dict[str, Any], image_id: str) -> Segment:
    if "index" not in raw:
        raise ValueError(f"image {image_id!r}: every segment needs an 'index'")
    return Segment(
        index=raw["index"],
        status=raw.get("status", "not_imaged"),
        digest=raw.get("digest"),
        fill_byte=raw.get("fill_byte"),
    )


def _image_from_json(raw: Dict[str, Any]) -> Image:
    required = ("image_id", "device_identity", "capture_id", "segment_size", "segments")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValueError(f"image entry is missing keys: {', '.join(missing)}")
    segments = tuple(_segment_from_json(seg, raw["image_id"]) for seg in raw["segments"])
    return Image(
        image_id=raw["image_id"],
        device_identity=raw["device_identity"],
        capture_id=raw["capture_id"],
        segment_size=raw["segment_size"],
        segments=segments,
        derived_from=raw.get("derived_from"),
    )


def load_library(path: Path) -> ImageLibrary:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        images = payload.get("images", [])
    else:
        images = payload
    if not isinstance(images, list):
        raise ValueError("'images' must be a list")
    library = ImageLibrary()
    for raw in images:
        if not isinstance(raw, dict):
            raise ValueError("each image entry must be an object")
        library.add_image(_image_from_json(raw))
    return library


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m forensic_segments",
        description="Select byte ranges across repeated images of one evidence disk.",
    )
    parser.add_argument("metadata", type=Path, help="JSON file describing the images")
    parser.add_argument("--start", type=int, required=True, help="first byte (inclusive)")
    parser.add_argument("--end", type=int, required=True, help="last byte (exclusive)")
    parser.add_argument(
        "--prefer",
        action="append",
        default=None,
        metavar="IMAGE_ID",
        help="image id to try first; may be repeated",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    return parser


def main(argv: "List[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        library = load_library(args.metadata)
        report = library.extract(args.start, args.end, preferred=args.prefer)
    except (ForensicSegmentsError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(report.to_json() if args.json else report.render())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
