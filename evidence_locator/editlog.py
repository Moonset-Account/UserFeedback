"""剪辑记录与证据引用的双向定位。

剪辑记录（EditLog）由若干剪辑事件组成，每个事件把某卷素材的一段
源区间 [source_in, source_out) 放到成片时间线的 [program_in, program_out)
上。所有区间均为含头不含尾，因此首尾相接的连续剪接点有唯一归属。

- 素材位置用（卷号, 日序, 当日帧号/时码）表示，允许跨午夜（入帧与出帧日序不同）。
- 同一段素材可以被复制进多个不同的成片区间，正向查询返回全部命中。
- 成片时间线同样用（日序, 当日帧号/时码）表示，反向查询返回精确到素材帧的来源。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from . import timecode as tc


class EditLogError(ValueError):
    """剪辑记录本身非法（区间倒置、成片区间重叠、参数非法等）。"""


class ProgramGapError(LookupError):
    """成片位置未被任何剪辑事件覆盖。"""


def _resolve_frame(value: int | str) -> int:
    """把当日帧号（int）或时码字符串解析为当日帧号。"""
    if isinstance(value, str):
        return tc.parse_timecode(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise EditLogError(f"帧号应为整数或时码字符串，收到 {value!r}")
    if not 0 <= value < tc.FRAMES_PER_DAY:
        raise EditLogError(
            f"帧号 {value} 非法：当日帧号应在 0..{tc.FRAMES_PER_DAY - 1} 之间"
        )
    return value


def _abs_frame(day_seq: int, frame: int | str) -> int:
    """（日序, 当日帧号/时码）→ 绝对帧号。"""
    if isinstance(day_seq, bool) or not isinstance(day_seq, int) or day_seq < 0:
        raise EditLogError(f"日序 {day_seq!r} 非法：应为非负整数")
    return day_seq * tc.FRAMES_PER_DAY + _resolve_frame(frame)


def _split_abs(abs_frame: int) -> tuple[int, int]:
    """绝对帧号 → (日序, 当日帧号)。"""
    return divmod(abs_frame, tc.FRAMES_PER_DAY)


@dataclass(frozen=True)
class _Event:
    reel: str
    source_in: int   # 绝对帧号，含
    source_out: int  # 绝对帧号，不含
    program_in: int  # 绝对帧号，含

    @property
    def duration(self) -> int:
        return self.source_out - self.source_in

    @property
    def program_out(self) -> int:
        """成片出帧（不含）。"""
        return self.program_in + self.duration


@dataclass(frozen=True)
class ProgramHit:
    """一次“素材 → 成片”命中：素材帧出现的成片位置。"""

    day_seq: int
    frame: int
    reel: str
    source_day_seq: int
    source_frame: int

    @property
    def timecode(self) -> str:
        """成片位置的时码。"""
        return tc.format_timecode(self.frame)

    @property
    def seconds(self) -> Fraction:
        """成片位置当日时刻的真实时长（秒，按 30000/1001）。"""
        return tc.frame_to_seconds(self.frame)


@dataclass(frozen=True)
class SourceHit:
    """一次“成片 → 素材”命中：成片位置对应的素材帧来源。"""

    reel: str
    day_seq: int
    frame: int
    program_day_seq: int
    program_frame: int

    @property
    def timecode(self) -> str:
        """素材帧的时码。"""
        return tc.format_timecode(self.frame)

    @property
    def seconds(self) -> Fraction:
        """素材帧当日时刻的真实时长（秒，按 30000/1001）。"""
        return tc.frame_to_seconds(self.frame)


class EditLog:
    """剪辑记录：维护剪辑事件并提供双向定位。"""

    def __init__(self) -> None:
        self._events: list[_Event] = []

    @property
    def events(self) -> tuple:
        """已登记的剪辑事件（按成片入帧排序，只读）。"""
        return tuple(self._events)

    def add_event(
        self,
        reel: str,
        source_in_day: int,
        source_in: int | str,
        source_out_day: int,
        source_out: int | str,
        program_in_day: int = 0,
        program_in: int | str = 0,
    ) -> None:
        """登记一条剪辑事件。

        参数：
            reel: 素材卷号。
            source_in_day / source_in: 素材入帧的日序与当日帧号（或时码字符串），含。
            source_out_day / source_out: 素材出帧的日序与当日帧号（或时码字符串），不含。
            program_in_day / program_in: 成片入帧的日序与当日帧号（或时码字符串），含。

        源区间可跨午夜（出帧日序大于入帧日序）。成片出帧由时长推出。
        所有区间含头不含尾；成片区间不得与已有事件重叠（首尾相接允许）。
        """
        if not isinstance(reel, str) or not reel:
            raise EditLogError(f"卷号 {reel!r} 非法：应为非空字符串")
        si = _abs_frame(source_in_day, source_in)
        so = _abs_frame(source_out_day, source_out)
        pi = _abs_frame(program_in_day, program_in)
        if so <= si:
            raise EditLogError(
                f"剪辑区间非法：含头不含尾区间要求出帧大于入帧，"
                f"入帧绝对帧号 {si}，出帧绝对帧号 {so}"
            )
        po = pi + (so - si)
        for e in self._events:
            if pi < e.program_out and e.program_in < po:
                raise EditLogError(
                    f"成片区间 [{pi}, {po}) 与已有事件（卷号 {e.reel}）的"
                    f"成片区间 [{e.program_in}, {e.program_out}) 重叠；"
                    f"连续剪接只允许首尾相接（含头不含尾）"
                )
        self._events.append(_Event(reel=reel, source_in=si, source_out=so, program_in=pi))
        self._events.sort(key=lambda e: e.program_in)

    def locate_program(
        self, reel: str, day_seq: int, frame: int | str
    ) -> list[ProgramHit]:
        """素材 → 成片：返回该素材帧出现的全部成片位置（按成片顺序）。

        同一段素材可能被复制进多个成片区间，故返回列表；无命中返回空列表。
        """
        target = _abs_frame(day_seq, frame)
        hits: list[ProgramHit] = []
        for e in self._events:
            if e.reel != reel:
                continue
            if e.source_in <= target < e.source_out:
                p = e.program_in + (target - e.source_in)
                p_day, p_frame = _split_abs(p)
                s_day, s_frame = _split_abs(target)
                hits.append(
                    ProgramHit(
                        day_seq=p_day,
                        frame=p_frame,
                        reel=reel,
                        source_day_seq=s_day,
                        source_frame=s_frame,
                    )
                )
        hits.sort(key=lambda h: (h.day_seq, h.frame))
        return hits

    def locate_source(self, day_seq: int, frame: int | str) -> SourceHit:
        """成片 → 素材：返回该成片位置精确到素材帧的来源。

        区间含头不含尾保证连续剪接点归属唯一。位置未被任何事件覆盖时
        抛出 ProgramGapError 并说明原因。
        """
        target = _abs_frame(day_seq, frame)
        for e in self._events:
            if e.program_in <= target < e.program_out:
                s = e.source_in + (target - e.program_in)
                s_day, s_frame = _split_abs(s)
                p_day, p_frame = _split_abs(target)
                return SourceHit(
                    reel=e.reel,
                    day_seq=s_day,
                    frame=s_frame,
                    program_day_seq=p_day,
                    program_frame=p_frame,
                )
        raise ProgramGapError(
            f"成片位置（日序 {day_seq}，当日帧号 {_resolve_frame(frame)}，"
            f"时码 {tc.format_timecode(_resolve_frame(frame))}）"
            f"不在任何剪辑事件的含头不含尾区间内"
        )
