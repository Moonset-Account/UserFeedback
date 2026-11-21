"""惰性参数服务：稀疏动量 SGD（带动量与权重衰减）的惰性行更新。

每步训练只触碰少量嵌入行。本服务在提交一步时只更新当步有梯度的行；
读取一个久未出现的行时，通过预先汇总的区间（2x2 仿射变换的组合）
一次性补齐中间所有零梯度步的效果，单行查询跨 N 个空步至多读取
O(log N) 个区间摘要。

逐行参考规则（mu 固定，mu 与 d 均在 [0, 1] 内）：

    v' = mu * v + g
    w' = d * w - eta * v'

未上报梯度的行视为 g = 0，其单步演化是线性变换：

    [w']   [d   -eta*mu] [w]
    [v'] = [0      mu  ] [v]

因此任意零梯度区间的效果可以预先汇总为三元组 (D, B, m)，作用为：

    w' = D * w + B * v
    v' = m * v

区间按“二进制计数器”方式在线合并成一棵持久化的区间森林，
后缀区间查询 [a..T] 至多分解为 2*log2(T) + O(1) 个已存摘要。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Hashable, List, Mapping, Optional, Tuple

__all__ = ["LazyParamService", "StepByStepParams"]


@dataclass
class _Node:
    """一个区间摘要：覆盖步区间 [start, end] 的零梯度复合变换。

    变换矩阵为 [[D, B], [0, m]]，即 w' = D*w + B*v, v' = m*v。
    left/right 为子节点在节点池中的下标，叶子为 -1。
    """

    start: int
    end: int
    D: float
    B: float
    m: float
    left: int = -1
    right: int = -1


def _check_unit_interval(name: str, value: float) -> float:
    value = float(value)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} 必须在 [0, 1] 内，得到 {value!r}")
    return value


class LazyParamService:
    """惰性参数服务。

    公开 API：
        LazyParamService(initial_weights, mu)  构造；每行初始 w 给定，v 初值为 0
        commit(eta, d, grads) -> int           提交一步，返回步号（从 1 起）
        read(row) -> (w, v)                    读取某行当前参数 w 与动量 v
        step                                   当前已提交的步数
        rows                                   全部行标识
        last_summary_reads                     最近一次 read 实际读取的区间摘要数
    """

    def __init__(self, initial_weights: Mapping[Hashable, float], mu: float):
        self._mu = _check_unit_interval("mu", mu)
        self._w: Dict[Hashable, float] = {
            k: float(v) for k, v in initial_weights.items()
        }
        self._v: Dict[Hashable, float] = {k: 0.0 for k in self._w}
        self._last: Dict[Hashable, int] = {k: 0 for k in self._w}
        self._step = 0
        self._nodes: List[_Node] = []
        self._roots: List[int] = []  # 区间森林的根，按时间先后排列
        self._last_summary_reads = 0

    # ------------------------------------------------------------------ API

    @property
    def step(self) -> int:
        return self._step

    @property
    def rows(self) -> Tuple[Hashable, ...]:
        return tuple(self._w)

    @property
    def last_summary_reads(self) -> int:
        """最近一次 read（或提交时的行补齐）实际读取的区间摘要数量。"""
        return self._last_summary_reads

    def commit(
        self,
        eta: float,
        d: float,
        grads: Optional[Mapping[Hashable, float]] = None,
    ) -> int:
        """提交一步：学习率 eta、衰减系数 d、稀疏梯度 grads（缺省行 g=0）。

        只更新当步有梯度的行；返回新步号。
        """
        eta = float(eta)
        if not eta >= 0.0:
            raise ValueError(f"eta 必须为非负有限数，得到 {eta!r}")
        d = _check_unit_interval("d", d)
        grads = grads or {}
        for row in grads:
            if row not in self._w:
                raise KeyError(f"未知行: {row!r}")

        t = self._step + 1
        for row, g in grads.items():
            self._catch_up(row, t - 1)
            v = self._mu * self._v[row] + float(g)
            self._v[row] = v
            self._w[row] = d * self._w[row] - eta * v
            self._last[row] = t

        # 记录本步的零梯度变换叶子，并按二进制计数器合并相邻等长区间
        self._append(_Node(t, t, d, -eta * self._mu, self._mu))
        self._step = t
        return t

    def read(self, row: Hashable) -> Tuple[float, float]:
        """读取某行当前 (w, v)；必要时补齐中间所有零梯度步。"""
        if row not in self._w:
            raise KeyError(f"未知行: {row!r}")
        self._catch_up(row, self._step)
        return self._w[row], self._v[row]

    # ------------------------------------------------------------- internal

    def _append(self, node: _Node) -> None:
        self._nodes.append(node)
        self._roots.append(len(self._nodes) - 1)
        # 二进制计数器合并：末尾两个区间等长则合并，保持森林中区间长度递减
        while len(self._roots) >= 2:
            right = self._nodes[self._roots[-1]]
            left = self._nodes[self._roots[-2]]
            if (left.end - left.start) != (right.end - right.start):
                break
            li, ri = self._roots[-2], self._roots[-1]
            # 复合变换 = 后区间 @ 先区间
            merged = _Node(
                left.start,
                right.end,
                right.D * left.D,
                right.D * left.B + right.B * left.m,
                right.m * left.m,
                left=li,
                right=ri,
            )
            self._nodes.append(merged)
            self._roots[-2:] = [len(self._nodes) - 1]

    def _range_nodes(self, a: int, b: int) -> List[int]:
        """把步区间 [a..b] 分解为已存区间摘要，按时间先后返回节点下标。"""
        out: List[int] = []
        for root_idx in self._roots:
            root = self._nodes[root_idx]
            if root.end < a or root.start > b:
                continue
            stack = [root_idx]
            while stack:
                i = stack.pop()
                nd = self._nodes[i]
                if nd.end < a or nd.start > b:
                    continue
                if nd.start >= a and nd.end <= b:
                    out.append(i)
                    continue
                # 部分重叠只会发生在内部节点（叶子是单步，要么含要么不含）
                stack.append(nd.right)
                stack.append(nd.left)
        return out

    def _catch_up(self, row: Hashable, upto: int) -> None:
        """把 row 从已同步步推进到第 upto 步（区间全为零梯度步）。"""
        last = self._last[row]
        if last >= upto:
            self._last_summary_reads = 0
            return
        nodes = self._range_nodes(last + 1, upto)
        D, B, m = 1.0, 0.0, 1.0
        for i in nodes:
            nd = self._nodes[i]
            # 新复合 = nd @ 已有复合
            D, B, m = nd.D * D, nd.D * B + nd.B * m, nd.m * m
        w, v = self._w[row], self._v[row]
        self._w[row] = D * w + B * v
        self._v[row] = m * v
        self._last[row] = upto
        self._last_summary_reads = len(nodes)


class StepByStepParams:
    """逐步更新的参考实现：每步对全部行应用规则，用于比对验证。"""

    def __init__(self, initial_weights: Mapping[Hashable, float], mu: float):
        self._mu = _check_unit_interval("mu", mu)
        self.w: Dict[Hashable, float] = {
            k: float(v) for k, v in initial_weights.items()
        }
        self.v: Dict[Hashable, float] = {k: 0.0 for k in self.w}
        self.step = 0

    def commit(
        self, eta: float, d: float, grads: Optional[Mapping[Hashable, float]] = None
    ) -> int:
        eta = float(eta)
        d = _check_unit_interval("d", d)
        grads = grads or {}
        for row in self.w:
            g = float(grads.get(row, 0.0))
            self.v[row] = self._mu * self.v[row] + g
            self.w[row] = d * self.w[row] - eta * self.v[row]
        self.step += 1
        return self.step

    def read(self, row: Hashable) -> Tuple[float, float]:
        return self.w[row], self.v[row]
