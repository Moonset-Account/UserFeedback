"""丢帧（Drop-Frame）时码与帧号的双向换算。

题面约定的编号规则：

- 每秒 30 个编号（帧号 00-29），实际帧率为 30000/1001（约 29.97 fps）。
- 丢帧规则：每十分钟为一个周期，非整十分钟的每分钟开头跳过帧编号 00 和 01
  （即每分钟丢 2 个编号，但逢 10 的分钟不丢）。
- 时码按 24 小时循环；跨午夜（跨日）由“日序”（day_seq）区分，
  日序 n 的绝对帧号 = n * FRAMES_PER_DAY + 当日帧号。

帧号约定：本模块中的“帧号”指从当日 00:00:00;00 起、按实际经过帧数
连续计数的整数（0 .. FRAMES_PER_DAY-1），与时码编号一一对应。
"""

from __future__ import annotations

import re
from fractions import Fraction

NOMINAL_FPS = 30
"""时码编号速率：每秒 30 个编号。"""

ACTUAL_FRAME_RATE = Fraction(30000, 1001)
"""实际帧率 30000/1001 fps。"""

DROP_COUNT = 2
"""非整十分钟的每分钟开头跳过的编号个数（00 和 01）。"""

FRAMES_PER_MINUTE = NOMINAL_FPS * 60 - DROP_COUNT
"""普通（非整十）分钟内的实际帧数：1798。"""

FRAMES_PER_10_MINUTES = FRAMES_PER_MINUTE * 10 + DROP_COUNT
"""每十分钟周期内的实际帧数：17982（整十分钟不丢，补回 2 帧）。"""

FRAMES_PER_HOUR = FRAMES_PER_10_MINUTES * 6
"""每小时实际帧数：107892。"""

HOURS_PER_DAY = 24

FRAMES_PER_DAY = FRAMES_PER_HOUR * HOURS_PER_DAY
"""每天实际帧数：2589408。24 小时是 10 分钟的整数倍，故跨午夜编号规则无缝衔接。"""


class TimecodeError(ValueError):
    """时码或帧号非法。"""


class DroppedFrameNumberError(TimecodeError):
    """使用了被丢帧规则跳过的编号（非整十分钟开头的 00/01）。"""


def _check_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TimecodeError(f"{name}必须为整数，收到 {value!r}")


def components_to_frame(hours: int, minutes: int, seconds: int, frames: int) -> int:
    """把时码分量 (时, 分, 秒, 帧) 换算为当日帧号。

    非法输入抛出 TimecodeError 并说明原因；命中被跳过的丢帧编号时
    抛出 DroppedFrameNumberError。
    """
    _check_int("小时", hours)
    _check_int("分钟", minutes)
    _check_int("秒", seconds)
    _check_int("帧编号", frames)
    if not 0 <= hours < HOURS_PER_DAY:
        raise TimecodeError(f"小时 {hours} 非法：时码按 24 小时循环，应在 00-23 之间")
    if not 0 <= minutes < 60:
        raise TimecodeError(f"分钟 {minutes} 非法：应在 00-59 之间")
    if not 0 <= seconds < 60:
        raise TimecodeError(f"秒 {seconds} 非法：应在 00-59 之间")
    if not 0 <= frames < NOMINAL_FPS:
        raise TimecodeError(
            f"帧编号 {frames} 非法：每秒 30 个编号，应在 00-29 之间"
        )
    if seconds == 0 and frames < DROP_COUNT and minutes % 10 != 0:
        raise DroppedFrameNumberError(
            f"{hours:02d}:{minutes:02d}:{seconds:02d};{frames:02d} 不存在："
            f"丢帧时码在非整十分钟的每分钟开头跳过编号 00 和 01"
        )
    total_minutes = hours * 60 + minutes
    nominal = (total_minutes * 60 + seconds) * NOMINAL_FPS + frames
    dropped = DROP_COUNT * (total_minutes - total_minutes // 10)
    return nominal - dropped


def frame_to_components(frame: int) -> tuple[int, int, int, int]:
    """把当日帧号换算为时码分量 (时, 分, 秒, 帧)。"""
    _check_int("帧号", frame)
    if not 0 <= frame < FRAMES_PER_DAY:
        raise TimecodeError(
            f"帧号 {frame} 非法：当日帧号应在 0..{FRAMES_PER_DAY - 1} 之间"
        )
    blocks, rest = divmod(frame, FRAMES_PER_10_MINUTES)
    if rest >= DROP_COUNT:
        extra = DROP_COUNT * ((rest - DROP_COUNT) // FRAMES_PER_MINUTE)
    else:
        extra = 0
    nominal = frame + DROP_COUNT * 9 * blocks + extra
    nominal, f = divmod(nominal, NOMINAL_FPS)
    nominal, s = divmod(nominal, 60)
    h, m = divmod(nominal, 60)
    return h, m, s, f


def format_timecode(frame: int) -> str:
    """把当日帧号格式化为丢帧时码字符串 ``HH:MM:SS;FF``（帧号前用分号表示丢帧）。"""
    h, m, s, f = frame_to_components(frame)
    return f"{h:02d}:{m:02d}:{s:02d};{f:02d}"


_TIMECODE_RE = re.compile(r"^(\d{1,2}):(\d{1,2}):(\d{1,2})[:;](\d{1,2})$")


def parse_timecode(text: str) -> int:
    """解析时码字符串为当日帧号。

    接受 ``HH:MM:SS;FF`` 或 ``HH:MM:SS:FF`` 两种写法。
    非法格式或非法编号抛出 TimecodeError / DroppedFrameNumberError 并说明原因。
    """
    if not isinstance(text, str):
        raise TimecodeError(f"时码必须为字符串，收到 {text!r}")
    match = _TIMECODE_RE.match(text.strip())
    if match is None:
        raise TimecodeError(
            f"无法解析时码 {text!r}：应为 HH:MM:SS;FF 或 HH:MM:SS:FF 格式"
        )
    h, m, s, f = (int(group) for group in match.groups())
    return components_to_frame(h, m, s, f)


def frame_to_seconds(frame: int) -> Fraction:
    """按实际帧率 30000/1001 把帧数换算为真实时长（秒，精确分数）。"""
    _check_int("帧数", frame)
    return Fraction(frame, 1) / ACTUAL_FRAME_RATE
