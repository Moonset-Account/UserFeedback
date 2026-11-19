"""涉案资金核算库（整数分账流水）。

场景：专案组已标出一笔涉案入账，其余初始余额与入账均为普通资金。
每个账户内两类资金可以混合。输入为有严格次序的账目记录（入账 /
转账，含转出方、转入方、金额），不允许透支。

调查人员选定一组出账（转账）记录后，本库在**所有**同时满足以下条件的
资金分配中，计算该组记录可能包含的涉案金额总和的下界与上界，并各返回
一份达到边界的逐笔分配：

  * 逐笔守恒：每笔转账的涉案部分不超过其金额，账户任意时刻涉案余额
    非负且不超过其总余额（即普通资金部分也不透支）；
  * 严格次序：涉案资金只能随时间向前流动，一次转账后又回流时仍按
    先后处理；
  * 涉案资金总量守恒，等于被标记入账的金额。

注意：逐笔最大涉案额往往无法同时达到（涉案资金只有一份），因此上界
是对整组记录联合优化得到的，**不能**把各笔单独的最大值直接相加。

模型：时间展开网络上的整数最小费用流。
  * 每个账户 a 在每个账目时点 t（第 t 笔记录之后）设节点 (a, t)；
  * 持有边 (a, t-1) -> (a, t)，容量 [0, 该账户第 t 步后的总余额]，
    承载该账户此时的涉案余额——容量上界即“涉案不超过总余额”，
    与流量守恒共同保证普通资金部分也不透支；
  * 第 i 笔转账 (src -> dst, m) 对应边 (src, i-1) -> (dst, i)，
    容量 [0, m]，承载该笔转账中的涉案金额；
  * 被标记的涉案入账在对应时点注入等量供应；末端各账户可自由留存，
    因此任何合法流水下模型必然可行。
金额均为整数，网络矩阵全单模，最优流必为整数流。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

__all__ = ["Ledger", "LedgerError", "Record", "Bounds"]


class LedgerError(Exception):
    """非法输入（如透支、金额非法）或非法查询时抛出。"""


class Record(NamedTuple):
    """一条账目记录。tx_id 从 1 开始按入账次序编号。"""

    tx_id: int
    kind: str  # 'deposit'（入账）或 'transfer'（转账/出账）
    source: Optional[str]  # 转出方；入账时为 None（外部来源）
    target: str  # 转入方
    amount: int
    marked: bool  # 是否为被专案组标记的涉案入账


@dataclass(frozen=True)
class Bounds:
    """一组出账记录的涉案金额总和边界及达到边界的逐笔分配。

    minimum / maximum：该组记录涉案金额总和的下界 / 上界。
    min_allocation / max_allocation：达到下界 / 上界的一份完整逐笔分配，
    键为全部转账记录的 tx_id，值为该笔转账中的涉案金额（整数）。
    """

    minimum: int
    maximum: int
    min_allocation: Dict[int, int]
    max_allocation: Dict[int, int]


def _min_cost_flow(
    num_nodes: int,
    arcs: List[Tuple[int, int, int, int]],
    source: int,
    sink: int,
    need: int,
) -> Optional[Tuple[int, List[int]]]:
    """求从 source 到 sink 恰好输送 need 单位的最小费用流。

    arcs 为 (u, v, capacity, cost) 列表。返回 (最小费用, 每条弧的流量)，
    不可行时返回 None。逐次最短路增广（SPFA 求最短路，支持负费用），
    容量与费用均为整数，结果流量必为整数。
    """
    graph: List[List[list]] = [[] for _ in range(num_nodes)]
    refs: List[Tuple[int, int]] = []
    for u, v, cap, cost in arcs:
        refs.append((u, len(graph[u])))
        graph[u].append([v, len(graph[v]), cap, cost])
        graph[v].append([u, len(graph[u]) - 1, 0, -cost])

    flowed = 0
    total_cost = 0
    while flowed < need:
        dist: List[Optional[int]] = [None] * num_nodes
        prev_node = [-1] * num_nodes
        prev_edge = [-1] * num_nodes
        in_queue = [False] * num_nodes
        dist[source] = 0
        queue: deque[int] = deque([source])
        in_queue[source] = True
        while queue:
            u = queue.popleft()
            in_queue[u] = False
            du = dist[u]
            assert du is not None
            for i, edge in enumerate(graph[u]):
                v, _, cap, cost = edge
                if cap > 0:
                    nd = du + cost
                    if dist[v] is None or nd < dist[v]:
                        dist[v] = nd
                        prev_node[v] = u
                        prev_edge[v] = i
                        if not in_queue[v]:
                            in_queue[v] = True
                            queue.append(v)
        if dist[sink] is None:
            return None
        # 沿最短路按瓶颈容量增广
        f = need - flowed
        v = sink
        while v != source:
            edge = graph[prev_node[v]][prev_edge[v]]
            if edge[2] < f:
                f = edge[2]
            v = prev_node[v]
        v = sink
        while v != source:
            edge = graph[prev_node[v]][prev_edge[v]]
            edge[2] -= f
            graph[v][edge[1]][2] += f
            v = prev_node[v]
        total_cost += f * dist[sink]
        flowed += f

    flows = []
    for u, idx in refs:
        forward = graph[u][idx]
        flows.append(graph[forward[0]][forward[1]][2])
    return total_cost, flows


class Ledger:
    """整数分账流水核算器。

    使用顺序：
      1. set_initial_balance() 设置各账户初始（普通）余额；
      2. 按严格次序逐笔调用 deposit() / transfer()，其中恰好一笔
         deposit(marked=True) 为涉案入账；
      3. analyze(tx_ids) 对选定的一组转账记录求涉案金额总和的上下界。
    """

    def __init__(self) -> None:
        self._initial: Dict[str, int] = {}
        self._balances: Dict[str, int] = {}
        self._records: List[Record] = []
        self._marked_tx: Optional[int] = None

    # ------------------------------------------------------------------
    # 输入校验
    # ------------------------------------------------------------------
    @staticmethod
    def _check_account(name: object) -> str:
        if not isinstance(name, str) or not name:
            raise LedgerError("账户名必须是非空字符串")
        return name

    @staticmethod
    def _check_amount(amount: object, *, allow_zero: bool = False) -> int:
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise LedgerError("金额必须是整数")
        if allow_zero:
            if amount < 0:
                raise LedgerError("金额不能为负")
        elif amount <= 0:
            raise LedgerError("金额必须为正整数")
        return amount

    # ------------------------------------------------------------------
    # 流水录入（严格按调用次序）
    # ------------------------------------------------------------------
    def set_initial_balance(self, account: str, amount: int) -> None:
        """设置账户初始余额（普通资金）。每个账户只能设置一次。"""
        account = self._check_account(account)
        amount = self._check_amount(amount, allow_zero=True)
        if account in self._balances:
            raise LedgerError(f"账户 {account!r} 已存在，不能重复设置初始余额")
        self._initial[account] = amount
        self._balances[account] = amount

    def deposit(self, account: str, amount: int, *, marked: bool = False) -> int:
        """记录一笔入账，返回其 tx_id。marked=True 表示涉案入账（全流水唯一）。"""
        account = self._check_account(account)
        amount = self._check_amount(amount)
        if marked and self._marked_tx is not None:
            raise LedgerError("只能标记一笔涉案入账")
        if account not in self._balances:
            self._balances[account] = 0
            self._initial[account] = 0
        self._balances[account] += amount
        tx_id = len(self._records) + 1
        self._records.append(Record(tx_id, "deposit", None, account, amount, bool(marked)))
        if marked:
            self._marked_tx = tx_id
        return tx_id

    def transfer(self, source: str, target: str, amount: int) -> int:
        """记录一笔转账（出账），返回其 tx_id。余额不足时拒绝（不允许透支）。"""
        source = self._check_account(source)
        target = self._check_account(target)
        amount = self._check_amount(amount)
        if source == target:
            raise LedgerError("转出方与转入方不能是同一账户")
        if source not in self._balances:
            raise LedgerError(f"转出方账户 {source!r} 不存在")
        if target not in self._balances:
            raise LedgerError(f"转入方账户 {target!r} 不存在")
        if amount > self._balances[source]:
            raise LedgerError(
                f"账户 {source!r} 余额 {self._balances[source]} 不足以转出 {amount}，禁止透支"
            )
        self._balances[source] -= amount
        self._balances[target] += amount
        tx_id = len(self._records) + 1
        self._records.append(Record(tx_id, "transfer", source, target, amount, False))
        return tx_id

    # ------------------------------------------------------------------
    # 只读视图（供核算与外部校验）
    # ------------------------------------------------------------------
    @property
    def records(self) -> Tuple[Record, ...]:
        return tuple(self._records)

    @property
    def accounts(self) -> frozenset:
        return frozenset(self._balances)

    @property
    def initial_balances(self) -> Dict[str, int]:
        return dict(self._initial)

    def balance(self, account: str) -> int:
        account = self._check_account(account)
        if account not in self._balances:
            raise LedgerError(f"账户 {account!r} 不存在")
        return self._balances[account]

    # ------------------------------------------------------------------
    # 核算
    # ------------------------------------------------------------------
    def analyze(self, tx_ids: Iterable[int]) -> Bounds:
        """对选定的一组出账记录，求其涉案金额总和的上下界及达到边界的分配。"""
        if self._marked_tx is None:
            raise LedgerError("尚未标记涉案入账，无法核算")
        selected = self._validate_query(tx_ids)
        min_cost, min_alloc = self._optimize(selected, sign=1)
        max_neg_cost, max_alloc = self._optimize(selected, sign=-1)
        return Bounds(
            minimum=min_cost,
            maximum=-max_neg_cost,
            min_allocation=min_alloc,
            max_allocation=max_alloc,
        )

    def _validate_query(self, tx_ids: Iterable[int]) -> Set[int]:
        try:
            iterator = iter(tx_ids)
        except TypeError:
            raise LedgerError("查询对象必须是一组交易编号") from None
        selected: Set[int] = set()
        for tx in iterator:
            if isinstance(tx, bool) or not isinstance(tx, int):
                raise LedgerError("交易编号必须是整数")
            if tx < 1 or tx > len(self._records):
                raise LedgerError(f"交易 {tx} 不存在")
            if self._records[tx - 1].kind != "transfer":
                raise LedgerError(f"交易 {tx} 不是出账转账记录，不能纳入查询组")
            selected.add(tx)
        return selected

    def _optimize(self, selected: Set[int], sign: int) -> Tuple[int, Dict[int, int]]:
        """建时间展开网络并求最小费用流。sign=+1 求下界，sign=-1 求上界。"""
        records = self._records
        n = len(records)
        accounts = sorted(self._balances)
        index = {a: i for i, a in enumerate(accounts)}
        width = n + 1

        def node(account: str, t: int) -> int:
            return index[account] * width + t

        source = len(accounts) * width
        sink = source + 1

        arcs: List[Tuple[int, int, int, int]] = []
        meta: List[Optional[int]] = []  # 与 arcs 对齐：转账弧记 tx_id，其余为 None
        balances = {a: self._initial[a] for a in accounts}
        marked_amount = 0

        for step, rec in enumerate(records, start=1):
            if rec.kind == "deposit":
                balances[rec.target] += rec.amount
                if rec.marked:
                    marked_amount = rec.amount
                    # 涉案入账：在对应时点注入等量供应
                    arcs.append((source, node(rec.target, step), rec.amount, 0))
                    meta.append(None)
            else:
                balances[rec.source] -= rec.amount
                balances[rec.target] += rec.amount
                cost = sign if rec.tx_id in selected else 0
                arcs.append(
                    (node(rec.source, step - 1), node(rec.target, step), rec.amount, cost)
                )
                meta.append(rec.tx_id)
            # 持有边：容量为该账户本步之后的总余额（涉案余额不得超过总余额）
            for a in accounts:
                arcs.append((node(a, step - 1), node(a, step), balances[a], 0))
                meta.append(None)
        for a in accounts:
            arcs.append((node(a, n), sink, marked_amount, 0))
            meta.append(None)

        result = _min_cost_flow(len(accounts) * width + 2, arcs, source, sink, marked_amount)
        if result is None:
            raise LedgerError("不存在满足余额与守恒约束的资金分配")
        cost, flows = result
        allocation: Dict[int, int] = {}
        for tx_id, flow in zip(meta, flows):
            if tx_id is not None:
                allocation[tx_id] = flow
        return cost, allocation
