"""证据引用双向定位库。

公开 API：
- timecode 模块：丢帧时码与帧号换算（components_to_frame / frame_to_components /
  parse_timecode / format_timecode / frame_to_seconds），
  异常 TimecodeError / DroppedFrameNumberError。
- editlog 模块：EditLog（add_event / locate_program / locate_source），
  结果类型 ProgramHit / SourceHit，异常 EditLogError / ProgramGapError。
"""

from . import timecode
from .editlog import (
    EditLog,
    EditLogError,
    ProgramGapError,
    ProgramHit,
    SourceHit,
)
from .timecode import (
    ACTUAL_FRAME_RATE,
    FRAMES_PER_DAY,
    DroppedFrameNumberError,
    TimecodeError,
    components_to_frame,
    format_timecode,
    frame_to_components,
    frame_to_seconds,
    parse_timecode,
)

__all__ = [
    "timecode",
    "EditLog",
    "EditLogError",
    "ProgramGapError",
    "ProgramHit",
    "SourceHit",
    "ACTUAL_FRAME_RATE",
    "FRAMES_PER_DAY",
    "DroppedFrameNumberError",
    "TimecodeError",
    "components_to_frame",
    "format_timecode",
    "frame_to_components",
    "frame_to_seconds",
    "parse_timecode",
]
