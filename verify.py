"""输出校验：检验归并输出是否为各分片最终行集的稳定归并。

期望输出定义为：各分片最终（重算完成后）全部行按 (key, seq) 排序的结果。
误过空洞（在空洞关闭前输出了被覆盖的键）会造成顺序回退、旧行重复或
内容缺失/多余，verify_output 会把这些偏差逐一报告出来。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import List, Optional, Sequence

from merge_operator import Row


@dataclass
class Discrepancy:
    index: int
    kind: str  # "order" | "duplicate" | "divergence"
    detail: str


@dataclass
class VerificationReport:
    discrepancies: List[Discrepancy]
    missing_seqs: List[int]          # 期望输出中缺失的 seq
    extra_seqs: List[int]            # 实际输出中多出的 seq（如重算前的旧行）
    first_divergence_index: Optional[int]

    @property
    def ok(self) -> bool:
        return (not self.discrepancies
                and not self.missing_seqs
                and not self.extra_seqs)


def expected_merge(shard_rows: Sequence[Sequence[Row]]) -> List[Row]:
    """各分片最终行集的标准稳定归并结果（按 (key, seq) 排序）。"""
    return sorted((r for rows in shard_rows for r in rows),
                  key=lambda r: (r.key, r.seq))


def verify_output(expected: Sequence[Row],
                  actual: Sequence[Row]) -> VerificationReport:
    """把实际输出与期望的稳定归并结果对比，报告全部偏差。"""
    discrepancies: List[Discrepancy] = []

    # 1) 顺序检查：(key, seq) 必须非降
    for i in range(1, len(actual)):
        prev = (actual[i - 1].key, actual[i - 1].seq)
        cur = (actual[i].key, actual[i].seq)
        if cur < prev:
            discrepancies.append(Discrepancy(
                i, "order",
                f"第 {i} 行 (key={cur[0]!r}, seq={cur[1]}) 排在 "
                f"(key={prev[0]!r}, seq={prev[1]}) 之后，输出顺序被破坏"))
            break

    # 2) 重复行检查（同一 seq 被输出两次，例如误过空洞后新旧行都输出）
    seen = set()
    for i, r in enumerate(actual):
        if r.seq in seen:
            discrepancies.append(Discrepancy(
                i, "duplicate", f"seq={r.seq} 被重复输出（第 {i} 行）"))
            break
        seen.add(r.seq)

    # 3) 内容检查：以 seq 为行的身份做多重集对比
    exp_counter = Counter(r.seq for r in expected)
    act_counter = Counter(r.seq for r in actual)
    missing = sorted((exp_counter - act_counter).elements())
    extra = sorted((act_counter - exp_counter).elements())

    # 4) 与期望序列逐位对比，定位首个分叉位置
    first_div: Optional[int] = None
    for i in range(max(len(expected), len(actual))):
        e = expected[i] if i < len(expected) else None
        a = actual[i] if i < len(actual) else None
        if e is None or a is None or (e.key, e.seq) != (a.key, a.seq):
            first_div = i
            if e is not None and a is not None:
                discrepancies.append(Discrepancy(
                    i, "divergence",
                    f"第 {i} 行应为 (key={e.key!r}, seq={e.seq})，"
                    f"实际为 (key={a.key!r}, seq={a.seq})"))
            break

    return VerificationReport(discrepancies, missing, extra, first_div)
