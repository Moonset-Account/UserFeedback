"""数据类型与常量。

证物库有数卷预编号封条，各卷独立编号、号段可能重复。每卷只能按开卷方向
依次使用；跳过的号码必须能由作废号段或未拍照的已登记封箱解释；单枚封条
仅能归属一个箱。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

#: 正向开卷：按 ``seals`` 的物理顺序（索引 0 -> n-1）依次使用。
DIRECTION_FORWARD = "forward"
#: 反向开卷：按 ``seals`` 的逆物理顺序（索引 n-1 -> 0）依次使用。
DIRECTION_REVERSE = "reverse"
_DIRECTIONS = (DIRECTION_FORWARD, DIRECTION_REVERSE)

#: 存在覆盖全部箱的合法分配。
STATUS_OK = "ok"
#: 每个箱单独看都有候选，但无法同时分配（顺序、唯一性或容量约束冲突）。
STATUS_CONFLICT = "conflict"
#: 至少有一个箱没有任何候选（号码/通配模式在任何非作废封条上都不匹配）。
STATUS_NO_CANDIDATE = "no_candidate"

MAX_ROLLS = 4
MAX_SEALS_PER_ROLL = 8


@dataclass(frozen=True)
class Roll:
    """一卷预编号封条。

    :param roll_id: 卷号（交接标识，需在全部卷中唯一）。
    :param seals: 卷上封条的物理顺序号码，由卷首到卷尾；各卷号段允许重复，
        但同一卷内号码不得重复。
    :param direction: 开卷方向，``forward`` / ``reverse``。
    :param void_ranges: 作废号段，形如 ``[("001", "002"), ("005", "005")]``，
        闭区间；端点必须是本卷存在的号码，数字号段按数值比较。
    """

    roll_id: str
    seals: Sequence[str]
    direction: str = DIRECTION_FORWARD
    void_ranges: Sequence[tuple[str, str]] = field(default_factory=tuple)


@dataclass(frozen=True)
class BoxRecord:
    """一次封箱登记记录，列表顺序即同一人员的封箱先后顺序。"""

    box_id: str
    #: 封箱照片抄录的局部号码；``None`` 表示未拍照但已登记的封箱。
    #: ``?`` 匹配单个数字，``*`` 匹配任意长度（含空）数字串，其余按字面匹配。
    partial_number: Optional[str] = None


@dataclass(frozen=True)
class SealRef:
    """一枚封条在某卷上的位置引用。"""

    roll_id: str
    #: 1 起的使用位置：本卷开卷使用顺序上的第几枚（作废封条同样占位）。
    usage_position: int
    #: 1 起的物理位置：封条在 ``Roll.seals`` 中的序号（与开卷方向无关）。
    physical_position: int
    #: 封条上印刷的号码。
    seal_number: str


@dataclass(frozen=True)
class BoxResult:
    """单个箱的核对结果，顺序与输入的 :class:`BoxRecord` 一致。"""

    box_id: str
    #: 该箱在全部合法分配中可能的封条位置；空元组表示无候选。
    possibilities: tuple[SealRef, ...]
    #: 是否唯一确定（仅一种可能归属）。
    determined: bool


@dataclass(frozen=True)
class MatchResult:
    """整批交接的核对结果。"""

    status: str
    boxes: tuple[BoxResult, ...]
    #: 合法分配总数（``ok`` 时 >= 1；其余状态为 0）。
    allocation_count: int

    @property
    def determined_all(self) -> bool:
        """``ok`` 且每个箱都唯一确定时为真。"""
        return self.status == STATUS_OK and all(b.determined for b in self.boxes)
