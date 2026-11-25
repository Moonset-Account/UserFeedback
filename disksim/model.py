"""磁盘仿真的数据模型与计时规则。

时间单位为整数 tick。盘片以固定速度旋转，用单调递增的旋转位置
``rotation_position``（tick 计数）描述：经过 ``sectors_per_track`` 个
tick 盘片转一整圈。磁头在寻道期间盘片仍然转动，因此寻道结束时的
旋转位置就是当前时间本身（按圈长取模后得到当前磁头下的扇区）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 调度模式，记录在 decision 事件的 reason 中。
MODE_SAFE = "safe"          # 存在可保住全部截止期的顺序
MODE_BEST_EFFORT = "best_effort"  # 已无全保方案，按降级规则择优
MODE_NORMAL = "normal"      # 普通 SCAN 首选即可全保（safe 的常见情形）


@dataclass(frozen=True)
class DiskConfig:
    """磁盘几何与计时参数。

    Attributes:
        cylinders: 柱面总数，柱面编号为 0 .. cylinders-1。
        sectors_per_track: 每磁道扇区数，扇区编号为 0 .. sectors-1。
        ticks_per_sector: 读一个扇区（扇区从磁头下经过）所需 tick 数。
        seek_ticks_per_cylinder: 每跨越一个柱面的寻道 tick 数。
        max_queue: 待处理队列上限，默认 8。
        head_cylinder: 磁头初始柱面。
    """

    cylinders: int
    sectors_per_track: int
    ticks_per_sector: int = 1
    seek_ticks_per_cylinder: int = 1
    max_queue: int = 8
    head_cylinder: int = 0

    def __post_init__(self) -> None:
        if self.cylinders <= 0:
            raise ValueError("cylinders 必须为正整数")
        if self.sectors_per_track <= 0:
            raise ValueError("sectors_per_track 必须为正整数")
        if self.ticks_per_sector <= 0:
            raise ValueError("ticks_per_sector 必须为正整数")
        if self.seek_ticks_per_cylinder <= 0:
            raise ValueError("seek_ticks_per_cylinder 必须为正整数")
        if self.max_queue <= 0:
            raise ValueError("max_queue 必须为正整数")
        if not (0 <= self.head_cylinder < self.cylinders):
            raise ValueError("head_cylinder 超出柱面范围")

    @property
    def rotation_period(self) -> int:
        """盘片转一整圈所需 tick 数。"""
        return self.sectors_per_track * self.ticks_per_sector

    def seek_ticks(self, from_cylinder: int, to_cylinder: int) -> int:
        """两柱面间的寻道耗时（与跨越柱面数成正比）。"""
        return abs(from_cylinder - to_cylinder) * self.seek_ticks_per_cylinder

    def head_sector_at(self, time: int) -> int:
        """给定时刻磁头正下方的扇区编号。"""
        return (time // self.ticks_per_sector) % self.sectors_per_track


@dataclass(frozen=True)
class Request:
    """一个不可中断的读请求。

    Attributes:
        rid: 请求编号（唯一，用于同分时按编号序列择优）。
        cylinder: 目标柱面。
        sector: 起始扇区。
        length: 连续读取的扇区数。
        arrival: 到达时刻（tick）。
        deadline: 最迟完成时刻（tick），在该时刻完成视为按时。
    """

    rid: int
    cylinder: int
    sector: int
    length: int
    arrival: int
    deadline: int

    def __post_init__(self) -> None:
        if self.length <= 0:
            raise ValueError(f"请求 {self.rid} 的 length 必须为正整数")
        if self.arrival < 0:
            raise ValueError(f"请求 {self.rid} 的 arrival 不能为负")
        if self.deadline < self.arrival:
            raise ValueError(f"请求 {self.rid} 的 deadline 早于 arrival")

    def validate_geometry(self, config: DiskConfig) -> None:
        if not (0 <= self.cylinder < config.cylinders):
            raise ValueError(f"请求 {self.rid} 的柱面 {self.cylinder} 越界")
        if not (0 <= self.sector < config.sectors_per_track):
            raise ValueError(f"请求 {self.rid} 的扇区 {self.sector} 越界")
        if self.length > config.sectors_per_track:
            raise ValueError(
                f"请求 {self.rid} 的长度 {self.length} 超过每磁道扇区数"
            )
        if self.sector + self.length > config.sectors_per_track:
            raise ValueError(f"请求 {self.rid} 跨磁道尾界，不支持环绕读取")

    @property
    def read_ticks(self) -> int:
        """纯数据读取耗时（等待旋转的时间不计入）。"""
        return self.length  # 与 ticks_per_sector 相乘在 plan_service 中完成


@dataclass(frozen=True)
class TimelineEvent:
    """时间线上的一个事件。

    kind 取值：
      - ``arrival``: 请求到达（含 ``accepted`` 字段，False 表示队列满被拒）。
      - ``decision``: 调度决策（含 ``mode`` 与候选评估说明）。
      - ``seek``: 一次寻道（同柱面距离为 0 时不产生该事件）。
      - ``rotational_wait``: 寻道结束后等待目标扇区转来。
      - ``sector_miss``: 寻道期间盘片转动，磁头刚好错过目标扇区。
      - ``read``: 不可中断的数据读取。
      - ``idle``: 队列为空，磁头空闲至下一到达时刻。
    """

    time: int
    kind: str
    detail: str = ""
    rid: int | None = None
    accepted: bool | None = None
    mode: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ServiceRecord:
    """一个请求的完整服务记录。"""

    rid: int
    arrival: int
    deadline: int
    cylinder: int
    sector: int
    length: int
    seek_start: int
    seek_end: int
    read_start: int
    complete: int
    seek_distance: int
    rotational_latency: int
    just_missed: bool
    on_time: bool


@dataclass(frozen=True)
class SimulationResult:
    """一次仿真的全部产出。"""

    records: tuple[ServiceRecord, ...]
    timeline: tuple[TimelineEvent, ...]
    rejected: tuple[int, ...]
    served_order: tuple[int, ...]

    @property
    def on_time_count(self) -> int:
        return sum(1 for r in self.records if r.on_time)

    @property
    def total_seek_distance(self) -> int:
        return sum(r.seek_distance for r in self.records)

    def record_of(self, rid: int) -> ServiceRecord:
        for record in self.records:
            if record.rid == rid:
                return record
        raise KeyError(f"请求 {rid} 未被服务")
