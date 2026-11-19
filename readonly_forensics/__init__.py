"""只读删除文件取证恢复模块。

只根据调用方提供的映射、分块采集结果与分配/改写/记录缺口证据做判定，
不访问磁盘、不修改任何输入数据。公开入口见 :mod:`readonly_forensics.engine`。
"""

from .model import (
    ACQUISITION_ERROR,
    ACQUISITION_MISSING,
    ACQUISITION_SHORT,
    Allocation,
    Chunk,
    MAPPING_MISSING,
    OVERWRITTEN,
    REALLOCATED,
    RECOVERED,
    RECORD_GAP,
    Overwrite,
    RecordGapEvent,
    RecoveryCase,
)
from .engine import recover_file
from .report import RecoveryReport, Segment

__all__ = [
    "ACQUISITION_ERROR",
    "ACQUISITION_MISSING",
    "ACQUISITION_SHORT",
    "Allocation",
    "Chunk",
    "MAPPING_MISSING",
    "OVERWRITTEN",
    "REALLOCATED",
    "RECOVERED",
    "RECORD_GAP",
    "Overwrite",
    "RecordGapEvent",
    "RecoveryCase",
    "RecoveryReport",
    "Segment",
    "recover_file",
]
