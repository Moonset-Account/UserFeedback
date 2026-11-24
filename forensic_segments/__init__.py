"""Forensic image segment selection over repeated images of one evidence disk.

Typical use::

    from forensic_segments import Image, ImageLibrary, Segment, SegmentStatus

    lib = ImageLibrary()
    lib.add_image(Image(
        image_id="acq2.img",
        device_identity="WD-WCC.../SN123",
        capture_id="CAP-2026-11-24-A",      # original acquisition number
        segment_size=512,
        segments=(
            Segment(0, SegmentStatus.READ, digest="ab12..."),
            Segment(1, SegmentStatus.UNREADABLE, fill_byte=0x00),
        ),
        derived_from=None,
    ))

    report = lib.extract(0, 1024, preferred=["acq2.img"])
    print(report.render())

See ``README.md`` for the full API and the guarantees each field carries.
"""

from .errors import (
    ForensicSegmentsError,
    SelectionError,
    UnknownSourceError,
    ValidationError,
)
from .library import ImageLibrary
from .models import (
    Candidate,
    FillNote,
    Image,
    Segment,
    SegmentSelection,
    SegmentStatus,
    SelectionReport,
)

__all__ = [
    "Candidate",
    "FillNote",
    "ForensicSegmentsError",
    "Image",
    "ImageLibrary",
    "Segment",
    "SegmentSelection",
    "SegmentStatus",
    "SelectionError",
    "SelectionReport",
    "UnknownSourceError",
    "ValidationError",
]

__version__ = "1.0.0"
