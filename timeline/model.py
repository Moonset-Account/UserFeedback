"""领域模型：设备、记录、重启区间、锚点、时间范围与冲突报告。

时间戳约定（本地合成数据，无外部依赖）：
- 设备本地时钟读数使用浮点秒数（任意单调刻度均可，单位由调用方保证一致）。
- 真实时间变量 t = 本地读数 + 段偏移 + 观测误差，误差以对称区间建模。
- 每次重启把一台设备的时钟切成互不相关的若干 *段(segment)*，段间偏移独立。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, order=True)
class SegmentKey:
    """设备重启后的时钟段标识，seg_index 从 0 开始。"""

    device_id: str
    seg_index: int

    def __str__(self) -> str:  # pragma: no cover - 仅用于报告可读性
        return f"{self.device_id}#seg{self.seg_index}"


@dataclass
class Device:
    device_id: str
    kind: str = "generic"
    # 重启时刻（该设备本地时钟读数），升序；记录据此被归入不同段
    reboots: list[float] = field(default_factory=list)

    def segment_index_for(self, local_ts: float) -> int:
        """返回 local_ts 落在第几段；恰在重启时刻的记录归入重启后的新段。"""
        idx = 0
        for rb in sorted(self.reboots):
            if local_ts >= rb:
                idx += 1
            else:
                break
        return idx


@dataclass
class Record:
    """一条来源记录。

    local_ts: 设备本地时钟读数。
    obs_error: 观测误差半径（秒），真实时间位于 ±obs_error 内。
    source: 溯源标识（如取证工作站导出文件中的行号/事件ID）。
    """

    record_id: str
    device_id: str
    local_ts: float
    obs_error: float = 0.0
    source: str = ""

    def __post_init__(self) -> None:
        if self.obs_error < 0:
            raise ValueError("obs_error 不能为负")
        if not self.source:
            self.source = self.record_id


@dataclass(frozen=True)
class SharedEventAnchor:
    """共同观测事件锚点：两台设备在同一真实时刻各自观测到一条记录。

    tolerance 为两设备观测之间允许的最大真实时间差（设备响应误差等）。
    """

    anchor_id: str
    record_a: str
    record_b: str
    tolerance: float = 0.0
    source: str = ""

    def __post_init__(self) -> None:
        if self.tolerance < 0:
            raise ValueError("tolerance 不能为负")
        if not self.source:
            object.__setattr__(self, "source", self.anchor_id)


@dataclass(frozen=True)
class AbsoluteAnchor:
    """绝对时间锚点：某条记录的真实时间被约束在 [abs_ts-tol, abs_ts+tol]。"""

    anchor_id: str
    record_id: str
    abs_ts: float
    tolerance: float = 0.0
    source: str = ""

    def __post_init__(self) -> None:
        if self.tolerance < 0:
            raise ValueError("tolerance 不能为负")
        if not self.source:
            object.__setattr__(self, "source", self.anchor_id)


@dataclass(frozen=True)
class Ordering:
    """来源记录明确给出的先后关系：before 必须早于 after。

    min_gap: 两条记录真实时间之间至少相隔的秒数（默认 0，仅要求不晚于）。
    """

    before: str
    after: str
    min_gap: float = 0.0
    source: str = ""

    def __post_init__(self) -> None:
        if self.min_gap < 0:
            raise ValueError("min_gap 不能为负")
        if not self.source:
            object.__setattr__(self, "source", f"order:{self.before}<{self.after}")


@dataclass(frozen=True)
class TimeRange:
    """一条记录在真实时间轴上的可能区间 [lower, upper]，inf 表示无界。"""

    record_id: str
    lower: float
    upper: float
    segment: SegmentKey

    @property
    def width(self) -> float:
        return self.upper - self.lower

    def overlaps(self, other: TimeRange, eps: float = 1e-12) -> bool:
        """区间相交（端点相触也算相交，由调用方结合因果依据另行判断）。"""
        return not (self.upper < other.lower - eps or other.upper < self.lower - eps)


@dataclass(frozen=True)
class IntervalClash:
    """矛盾锚点在同一段上的偏移区间冲突描述。"""

    segment: SegmentKey
    anchor_a: str
    interval_a: tuple[float, float]
    anchor_b: str
    interval_b: tuple[float, float]
    sources: tuple[str, ...]


@dataclass(frozen=True)
class ConflictReport:
    """锚点互相矛盾时的完整冲突说明。"""

    message: str
    # 负环上涉及的源记录/锚点/排序来源
    conflicting_sources: tuple[str, ...]
    # 涉及的设备段
    conflicting_segments: tuple[SegmentKey, ...]
    # 逐锚点隔离求解得到的偏移可行区间（可能含无穷端点）
    anchor_intervals: dict[str, tuple[SegmentKey, tuple[float, float]]]
    # 两两区间交集为空的具体冲突
    interval_clashes: tuple[IntervalClash, ...]


class ConflictError(Exception):
    """新增锚点导致约束系统不可行（出现负环）时抛出。"""

    def __init__(self, report: ConflictReport) -> None:
        super().__init__(report.message)
        self.report = report
