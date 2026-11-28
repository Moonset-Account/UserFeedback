"""面向分段字节流的跨版本帧归并模块。

公开 API 见 ``frame_merger.catalog``、``frame_merger.merger`` 与
``frame_merger.codec``，并在本包根命名空间重新导出。
"""

from .catalog import (
    CONTROL_MARKER,
    FRAME_MARKER,
    INCOMPLETE,
    REJECTED,
    FrameCatalog,
    ParseOutcome,
    VersionSpec,
    crc8,
    sum8,
)
from .codec import (
    build_ambiguous_v1_v2,
    build_control_packet,
    encode_frame,
    encode_measurement,
)
from .merger import (
    DiscontinuityReason,
    FrameEvent,
    FrameMerger,
    GapEvent,
    VersionEvent,
)

__all__ = [
    "CONTROL_MARKER",
    "FRAME_MARKER",
    "INCOMPLETE",
    "REJECTED",
    "DiscontinuityReason",
    "FrameCatalog",
    "FrameEvent",
    "FrameMerger",
    "GapEvent",
    "ParseOutcome",
    "VersionEvent",
    "VersionSpec",
    "build_ambiguous_v1_v2",
    "build_control_packet",
    "crc8",
    "encode_frame",
    "encode_measurement",
    "sum8",
]
