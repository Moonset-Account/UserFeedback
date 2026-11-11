"""RIP/1.0 科研增量补丁采集器（固定协议子集，见 docs/PROTOCOL.md）。"""
from .engine import ERROR_MARKER, CollectorEngine, IngestDecision
from .parser import PROTOCOL_VERSION, ParseError, parse_line
from .paths import PathError
from .transport import run_file_collection, run_http_collection

__all__ = [
    "CollectorEngine",
    "IngestDecision",
    "ParseError",
    "PathError",
    "PROTOCOL_VERSION",
    "ERROR_MARKER",
    "parse_line",
    "run_file_collection",
    "run_http_collection",
]
