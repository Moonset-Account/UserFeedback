"""分位数归并器：分层直方图约束下的分位数极值与见证。

公开 API 概览（详见 README.md）：

数据记录
    Bucket, Observation
版本化存储
    VersionedStore, Change
计算与见证
    SegmentModel, Witness, WitnessEntry, Allocation, RLE
    WindowResult, MusReport, VerificationReport
异常
    ValidationError, InfeasibleError, HistoryMissingError, KOutOfRangeError
"""

from .records import Bucket, Observation, ValidationError
from .model import (
    SegmentModel,
    InfeasibleError,
    KOutOfRangeError,
    Witness,
    WitnessEntry,
    Allocation,
    RLE,
    WindowResult,
    MusReport,
)
from .store import VersionedStore, Change, HistoryMissingError
from .witness import verify_witness, VerificationReport
from .mus import find_mus

__all__ = [
    "Bucket",
    "Observation",
    "ValidationError",
    "SegmentModel",
    "InfeasibleError",
    "KOutOfRangeError",
    "Witness",
    "WitnessEntry",
    "Allocation",
    "RLE",
    "WindowResult",
    "MusReport",
    "VersionedStore",
    "Change",
    "HistoryMissingError",
    "verify_witness",
    "VerificationReport",
    "find_mus",
]
