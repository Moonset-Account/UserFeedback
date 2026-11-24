"""数据模型与输入校验。

约定：
- 匹配（matching）用长度 n_in 的元组表示，matching[i] 为输入 i 本拍连接的输出，
  未参与连接记为 -1。每个输出至多出现一次（部分匹配）。
- 拍（beat）编号在一帧内从 0 开始。每项业务 (i, j) 的允许拍区间为闭区间 [l, r]。
- 每拍每条有效连线至多搬 1 单位需求；禁止提前或超量发送。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

NO_PORT = -1
MAX_PORTS = 6


class InfeasibleError(Exception):
    """不存在可精确清空需求矩阵的可行排程。

    属性 missed 为不可行窗口中首个已错过的需求（MissedDemand），可能为 None。
    """

    def __init__(self, message: str, missed: "MissedDemand | None" = None):
        super().__init__(message)
        self.missed = missed


class IllegalPlanError(Exception):
    """回放时发现发给设备的连线/发数指令本身不合法（而非无可行解）。"""


@dataclass(frozen=True)
class MissedDemand:
    """首个已错过窗口的需求。"""

    i: int
    j: int
    window: tuple[int, int]
    remaining: int
    missed_beat: int  # 窗口关闭拍 r；该拍结束后仍有剩余即视为错过

    def __str__(self) -> str:
        l, r = self.window
        return (
            f"需求 ({self.i},{self.j}) 窗口 [{l},{r}] 在第 {self.missed_beat} 拍关闭，"
            f"仍剩 {self.remaining} 单位未送"
        )


@dataclass(frozen=True)
class BeatRecord:
    """一拍可回放的完整记录。"""

    beat: int
    matching: tuple[int, ...]          # 本拍目标匹配
    changed_ports: tuple[int, ...]     # 与上拍目标不同的输入端口（当拍不得发数）
    active_edges: tuple[tuple[int, int], ...]  # 两拍保持不变、本拍可发数的连线
    sends: tuple[tuple[int, int], ...]         # 本拍实际发数的边（每条 1 单位）
    idle_edges: tuple[tuple[int, int], ...]    # 可发但留空的有效连线

    def to_dict(self) -> dict:
        return {
            "beat": self.beat,
            "matching": list(self.matching),
            "changed_ports": list(self.changed_ports),
            "active_edges": [list(e) for e in self.active_edges],
            "sends": [list(e) for e in self.sends],
            "idle_edges": [list(e) for e in self.idle_edges],
        }


@dataclass(frozen=True)
class Schedule:
    """一帧的完整拍序。"""

    records: tuple[BeatRecord, ...]
    total_changes: int                 # 全帧连线变更端口次数（含初始建立）
    demand: tuple[tuple[int, ...], ...]
    forbidden: frozenset[tuple[int, int]]
    windows: Mapping[tuple[int, int], tuple[int, int]]
    initial_matching: tuple[int, ...]

    @property
    def num_beats(self) -> int:
        return len(self.records)

    def matchings(self) -> list[tuple[int, ...]]:
        return [r.matching for r in self.records]

    def sends_by_beat(self) -> list[tuple[tuple[int, int], ...]]:
        return [r.sends for r in self.records]

    def to_dict(self) -> dict:
        return {
            "num_beats": self.num_beats,
            "total_changes": self.total_changes,
            "records": [r.to_dict() for r in self.records],
        }


@dataclass(frozen=True)
class Problem:
    """一帧开关排程问题实例。

    参数：
        demand: n_in x n_out 非负整数需求矩阵。
        forbidden: 不可用交叉点 (i, j) 的集合。
        windows: 每项业务的允许拍区间 {(i, j): (l, r)}（闭区间，拍从 0 起）；
                 未给出的业务使用 default_window。
        default_window: 缺省允许区间，默认为 (0, 一个宽松上界)。
        previous_matching: 上帧结束时保留的连线，长度 n_in，-1 表示悬空。
    """

    n_in: int
    n_out: int
    demand: tuple[tuple[int, ...], ...]
    forbidden: frozenset[tuple[int, int]]
    windows: Mapping[tuple[int, int], tuple[int, int]]
    previous_matching: tuple[int, ...]
    default_window: tuple[int, int]

    @classmethod
    def build(
        cls,
        demand: Sequence[Sequence[int]],
        forbidden: Iterable[tuple[int, int]] = (),
        windows: Mapping[tuple[int, int], tuple[int, int]] | None = None,
        previous_matching: Sequence[int] | None = None,
        default_window: tuple[int, int] | None = None,
    ) -> "Problem":
        rows = [tuple(int(x) for x in row) for row in demand]
        if not rows:
            raise ValueError("需求矩阵至少需要 1 个输入端口")
        n_in = len(rows)
        n_out = len(rows[0])
        if n_in == 0 or n_out == 0:
            raise ValueError("输入/输出端口数必须为正")
        if n_in > MAX_PORTS or n_out > MAX_PORTS:
            raise ValueError(f"交换机最多 {MAX_PORTS} 入 {MAX_PORTS} 出")
        if any(len(row) != n_out for row in rows):
            raise ValueError("需求矩阵必须是规整矩阵")
        if any(x < 0 for row in rows for x in row):
            raise ValueError("需求必须非负")

        forb = frozenset((int(i), int(j)) for i, j in forbidden)
        for i, j in forb:
            if not (0 <= i < n_in and 0 <= j < n_out):
                raise ValueError(f"不可用交叉点 ({i},{j}) 越界")
            if rows[i][j] > 0:
                raise IllegalPlanError(
                    f"需求 ({i},{j}) 落在不可用交叉点上，无法传送"
                )

        total = sum(sum(row) for row in rows)
        if default_window is None:
            # 宽松上界：总量 + 一次全重配，足够覆盖任何可行解长度
            default_window = (0, max(0, total + n_in))
        l0, r0 = default_window
        if l0 < 0 or r0 < l0:
            raise ValueError("default_window 非法，要求 0 <= l <= r")

        win: dict[tuple[int, int], tuple[int, int]] = {}
        for (i, j), w in (windows or {}).items():
            if not (0 <= i < n_in and 0 <= j < n_out):
                raise ValueError(f"窗口业务 ({i},{j}) 越界")
            l, r = int(w[0]), int(w[1])
            if l < 0 or r < l:
                raise ValueError(f"业务 ({i},{j}) 窗口 [{l},{r}] 非法")
            if (i, j) in forb and rows[i][j] > 0:
                raise IllegalPlanError(
                    f"业务 ({i},{j}) 既在不可用交叉点上又有正需求"
                )
            win[(i, j)] = (l, r)
        # 对所有正需求补齐窗口，保证内部查表完整
        for i in range(n_in):
            for j in range(n_out):
                if rows[i][j] > 0 and (i, j) not in win:
                    win[(i, j)] = (l0, r0)
                if (i, j) in win:
                    l, r = win[(i, j)]
                    if rows[i][j] > r - l + 1:
                        # 静态即可判定窗口装不下需求
                        raise InfeasibleError(
                            f"业务 ({i},{j}) 需求 {rows[i][j]} 超过窗口 "
                            f"[{l},{r}] 的容量 {r - l + 1}",
                            MissedDemand(i, j, (l, r), rows[i][j], r),
                        )

        if previous_matching is None:
            prev = tuple(NO_PORT for _ in range(n_in))
        else:
            prev = tuple(int(x) for x in previous_matching)
            if len(prev) != n_in:
                raise ValueError("previous_matching 长度必须等于输入端口数")
            seen: set[int] = set()
            for i, j in enumerate(prev):
                if j == NO_PORT:
                    continue
                if not (0 <= j < n_out):
                    raise ValueError(f"previous_matching[{i}]={j} 越界")
                if j in seen:
                    raise ValueError("previous_matching 中同一输出被连接多次")
                seen.add(j)
                if (i, j) in forb:
                    raise IllegalPlanError(
                        f"保留连线 ({i},{j}) 位于不可用交叉点"
                    )

        return cls(
            n_in=n_in,
            n_out=n_out,
            demand=tuple(rows),
            forbidden=forb,
            windows=win,
            previous_matching=prev,
            default_window=(l0, r0),
        )

    def window_of(self, i: int, j: int) -> tuple[int, int]:
        return self.windows[(i, j)]

    def total_demand(self) -> int:
        return sum(sum(row) for row in self.demand)

    def demand_cells(self) -> tuple[tuple[int, int], ...]:
        return tuple(
            (i, j)
            for i in range(self.n_in)
            for j in range(self.n_out)
            if self.demand[i][j] > 0
        )
