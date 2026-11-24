"""Test helpers: build synthetic images without touching real media."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forensic_segments import (  # noqa: E402
    Image,
    ImageLibrary,
    Segment,
    SegmentStatus,
)

DEVICE_A = "NVMe-SN-ORIGINAL-0001"
DEVICE_B = "NVMe-SN-RETRY-0002"
SEG = 512


def read(index: int, digest: str) -> Segment:
    return Segment(index, SegmentStatus.READ, digest=digest)


def unreadable(index: int, fill: int = 0x00) -> Segment:
    return Segment(index, SegmentStatus.UNREADABLE, fill_byte=fill)


def not_imaged(index: int) -> Segment:
    return Segment(index, SegmentStatus.NOT_IMAGED)


def image(
    image_id: str,
    segments: Iterable[Segment],
    *,
    device: str = DEVICE_A,
    capture: Optional[str] = None,
    derived_from: Optional[str] = None,
    segment_size: int = SEG,
) -> Image:
    return Image(
        image_id=image_id,
        device_identity=device,
        capture_id=capture or f"CAP-{image_id}",
        segment_size=segment_size,
        segments=tuple(segments),
        derived_from=derived_from,
    )


def library(*images: Image) -> ImageLibrary:
    lib = ImageLibrary()
    for img in images:
        lib.add_image(img)
    return lib
