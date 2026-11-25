"""号码分配：冲突判定与最少中继号着色。

规则
----
预约可共用同一中继号，当且仅当双方号码在重叠的有效期和回拨冷却期内都
不会冲突。形式化地，预约 A、B 冲突（不可共用中继号）当且仅当：

1. 二者的占用区间（有效期加各自冷却期，半开）相交；并且
2. 二者至少共享一端号码。

已对客户公布的预约（``pinned_relay`` 非空）固定原号，其余预约在满足
约束的前提下求使用中继号最少的分配（冲突图最小着色，n <= 14 用
DSATUR 风格的精确回溯求解）。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Iterable, Optional, Sequence

from .models import Allocation, Reservation

MAX_RESERVATIONS = 14


class AllocationError(Exception):
    """分配相关错误的基类。"""


class InvalidReservationError(AllocationError):
    """预约数据本身不合法。"""


class ConflictingPinError(AllocationError):
    """两个被固定到同一中继号的已公布预约互相冲突。"""


class InsufficientRelaysError(AllocationError):
    """可用中继号池不足以完成分配。"""


def _extended_intervals_overlap(a: Reservation, b: Reservation) -> bool:
    """半开占用区间是否相交（端点相接不算重叠）。"""
    return a.start < b.occupied_end and b.start < a.occupied_end


def conflicts(a: Reservation, b: Reservation) -> bool:
    """返回两个预约是否不能共用同一中继号。"""
    if not _extended_intervals_overlap(a, b):
        return False
    parties_a = (a.caller, a.callee)
    return b.caller in parties_a or b.callee in parties_a


class RelayAllocator:
    """根据一批预约构造最少中继号的分配方案。

    Args:
        reservations: 至多 ``MAX_RESERVATIONS`` 条预约。
        relay_pool: 可选的可用中继号集合；给出后分配只能使用池内号码，
            固定原号也必须属于该池。为 ``None`` 时按需生成
            ``R01, R02, ...`` 形式的号码。
    """

    def __init__(
        self,
        reservations: Sequence[Reservation],
        relay_pool: Optional[Iterable[str]] = None,
    ) -> None:
        self._reservations = self._validate(reservations)
        self._pool: Optional[tuple[str, ...]] = (
            tuple(dict.fromkeys(relay_pool)) if relay_pool is not None else None
        )
        self._n = len(self._reservations)
        self._adj = self._build_conflict_graph()
        self._allocation: Optional[Allocation] = None

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate(reservations: Sequence[Reservation]) -> tuple[Reservation, ...]:
        if len(reservations) > MAX_RESERVATIONS:
            raise InvalidReservationError(
                f"预约数量 {len(reservations)} 超过上限 {MAX_RESERVATIONS}"
            )
        seen_ids: set[str] = set()
        validated: list[Reservation] = []
        for r in reservations:
            if not isinstance(r, Reservation):
                raise InvalidReservationError(f"非 Reservation 对象: {r!r}")
            if not r.reservation_id:
                raise InvalidReservationError("预约 ID 不能为空")
            if r.reservation_id in seen_ids:
                raise InvalidReservationError(f"预约 ID 重复: {r.reservation_id}")
            seen_ids.add(r.reservation_id)
            if not r.caller or not r.callee:
                raise InvalidReservationError(
                    f"预约 {r.reservation_id} 的主叫/被叫号码不能为空"
                )
            if r.caller == r.callee:
                raise InvalidReservationError(
                    f"预约 {r.reservation_id} 主叫与被叫号码相同"
                )
            if r.end <= r.start:
                raise InvalidReservationError(
                    f"预约 {r.reservation_id} 有效期终点必须晚于起点"
                )
            if r.cooldown < 0:
                raise InvalidReservationError(
                    f"预约 {r.reservation_id} 冷却期不能为负"
                )
            validated.append(r)
        return tuple(validated)

    def _build_conflict_graph(self) -> list[frozenset[int]]:
        adj: list[set[int]] = [set() for _ in range(self._n)]
        for i in range(self._n):
            for j in range(i + 1, self._n):
                if conflicts(self._reservations[i], self._reservations[j]):
                    adj[i].add(j)
                    adj[j].add(i)
        return [frozenset(nb) for nb in adj]

    # ------------------------------------------------------------------ #
    # 公开入口
    # ------------------------------------------------------------------ #
    @property
    def reservations(self) -> tuple[Reservation, ...]:
        return self._reservations

    def conflict_pairs(self) -> tuple[tuple[str, str], ...]:
        """所有冲突预约对的 ID（按注册顺序）。"""
        pairs: list[tuple[str, str]] = []
        for i in range(self._n):
            for j in self._adj[i]:
                if i < j:
                    pairs.append(
                        (
                            self._reservations[i].reservation_id,
                            self._reservations[j].reservation_id,
                        )
                    )
        return tuple(pairs)

    def allocate(self) -> Allocation:
        """计算并缓存最少中继号分配。"""
        if self._allocation is not None:
            return self._allocation

        fixed = self._prepare_fixed_colors()
        colors = self._minimum_coloring(fixed)
        self._allocation = self._build_allocation(colors, fixed)
        return self._allocation

    # ------------------------------------------------------------------ #
    # 固定号（已公布预约）
    # ------------------------------------------------------------------ #
    def _prepare_fixed_colors(self) -> dict[int, int]:
        """把固定中继号映射成颜色索引，并校验固定约束。"""
        label_to_color: dict[str, int] = {}
        fixed: dict[int, int] = {}
        for idx, r in enumerate(self._reservations):
            if r.pinned_relay is None:
                continue
            if self._pool is not None and r.pinned_relay not in self._pool:
                raise InvalidReservationError(
                    f"预约 {r.reservation_id} 固定中继号 {r.pinned_relay!r} "
                    f"不在可用号码池中"
                )
            color = label_to_color.setdefault(r.pinned_relay, len(label_to_color))
            for other_idx, other_color in fixed.items():
                if other_color == color and other_idx in self._adj[idx]:
                    other = self._reservations[other_idx]
                    raise ConflictingPinError(
                        f"已公布预约 {r.reservation_id} 与 {other.reservation_id} "
                        f"被固定到同一中继号 {r.pinned_relay!r}，但二者冲突"
                    )
            fixed[idx] = color
        return fixed

    # ------------------------------------------------------------------ #
    # 精确最小着色（DSATUR 风格回溯，n <= 14）
    # ------------------------------------------------------------------ #
    def _clique_lower_bound(self) -> int:
        """Bron–Kerbosch（pivot）求最大团，作为色数下界。"""
        best = 0
        adj = self._adj

        def bron_kerbosch(
            r: frozenset[int], p: frozenset[int], x: frozenset[int]
        ) -> None:
            nonlocal best
            if not p and not x:
                best = max(best, len(r))
                return
            if len(r) + len(p) <= best:
                return
            pivot = max(p | x, key=lambda v: len(adj[v] & p))
            for v in sorted(p - adj[pivot]):
                bron_kerbosch(r | {v}, p & adj[v], x & adj[v])
                p = p - {v}
                x = x | {v}

        if self._n:
            bron_kerbosch(frozenset(), frozenset(range(self._n)), frozenset())
        return best

    def _minimum_coloring(self, fixed: dict[int, int]) -> list[int]:
        n = self._n
        adj = self._adj
        if n == 0:
            return []
        lower = max(
            self._clique_lower_bound(),
            len(set(fixed.values())),
        )
        limit = max(lower, (max(fixed.values()) + 1) if fixed else 0)

        def search(
            colors: list[int],
            remaining: set[int],
            neighbor_colors: list[set[int]],
            cap: int,
        ) -> bool:
            if not remaining:
                return True
            # DSATUR：邻接已用颜色最多者优先，平局取度数最大、再取索引最小。
            vertex = max(
                remaining,
                key=lambda v: (len(neighbor_colors[v]), len(adj[v]), -v),
            )
            used = neighbor_colors[vertex]
            if vertex in fixed:
                if fixed[vertex] in used or fixed[vertex] >= cap:
                    return False
                candidates = (fixed[vertex],)
            else:
                candidates = sorted(c for c in range(cap) if c not in used)
            for color in candidates:
                colors[vertex] = color
                remaining.remove(vertex)
                touched: list[int] = []
                for nb in adj[vertex]:
                    if nb in remaining and color not in neighbor_colors[nb]:
                        neighbor_colors[nb].add(color)
                        touched.append(nb)
                if search(colors, remaining, neighbor_colors, cap):
                    return True
                for nb in touched:
                    neighbor_colors[nb].discard(color)
                remaining.add(vertex)
                colors[vertex] = -1
            return False

        while True:
            colors = [-1] * n
            neighbor_colors = [set() for _ in range(n)]
            for idx, color in fixed.items():
                colors[idx] = color
                for nb in adj[idx]:
                    neighbor_colors[nb].add(color)
            remaining = {i for i in range(n) if i not in fixed}
            if search(colors, remaining, neighbor_colors, limit):
                return colors
            limit += 1

    # ------------------------------------------------------------------ #
    # 结果构造
    # ------------------------------------------------------------------ #
    def _fixed_labels(self, fixed: dict[int, int]) -> dict[int, str]:
        labels: dict[int, str] = {}
        for idx, color in fixed.items():
            labels[color] = self._reservations[idx].pinned_relay  # type: ignore[assignment]
        return labels

    def _build_allocation(
        self, colors: list[int], fixed: dict[int, int]
    ) -> Allocation:
        used_colors = sorted(set(colors))
        fixed_labels = self._fixed_labels(fixed)

        if self._pool is not None:
            if len(used_colors) > len(self._pool):
                raise InsufficientRelaysError(
                    f"至少需要 {len(used_colors)} 个中继号，"
                    f"号码池仅有 {len(self._pool)} 个"
                )
            labels = {color: self._pool[color] for color in used_colors}
        else:
            labels = {
                color: fixed_labels.get(color, f"R{color + 1:02d}")
                for color in used_colors
            }

        mapping = OrderedDict(
            (r.reservation_id, labels[color])
            for r, color in zip(self._reservations, colors)
        )
        return Allocation(mapping=mapping, relays=frozenset(mapping.values()))
