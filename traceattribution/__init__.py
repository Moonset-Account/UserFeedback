"""培训素材分发副本的保护与归属模块。"""
from .core import (
    MARKER_TAG,
    Attribution,
    Marker,
    ScanResult,
    Status,
    TraceConfig,
    attribute,
    extract_body,
    protect_pack,
    protect_text,
    scan_file,
)

__all__ = [
    "MARKER_TAG",
    "Attribution",
    "Marker",
    "ScanResult",
    "Status",
    "TraceConfig",
    "attribute",
    "extract_body",
    "protect_pack",
    "protect_text",
    "scan_file",
]
