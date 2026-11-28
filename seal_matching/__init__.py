"""证物库封条交接核对匹配模块的公开 API。

用法见 README。核心实现位于 :mod:`seal_matching.core`，数据类型位于
:mod:`seal_matching.model`。
"""

from __future__ import annotations

from .core import compile_pattern, match_seals
from .model import (
    DIRECTION_FORWARD,
    DIRECTION_REVERSE,
    MAX_ROLLS,
    MAX_SEALS_PER_ROLL,
    STATUS_CONFLICT,
    STATUS_NO_CANDIDATE,
    STATUS_OK,
    BoxRecord,
    BoxResult,
    MatchResult,
    Roll,
    SealRef,
)

__all__ = [
    "DIRECTION_FORWARD",
    "DIRECTION_REVERSE",
    "STATUS_OK",
    "STATUS_CONFLICT",
    "STATUS_NO_CANDIDATE",
    "MAX_ROLLS",
    "MAX_SEALS_PER_ROLL",
    "Roll",
    "BoxRecord",
    "SealRef",
    "BoxResult",
    "MatchResult",
    "match_seals",
    "compile_pattern",
]
