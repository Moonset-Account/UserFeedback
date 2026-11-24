"""见证的独立验算。

验算只依赖见证自身携带的观测快照与分配，不需要访问原存储，
因此旧修订上发给调用方的见证可以在任何时候独立复核。

检查项
======
1. 每个时间段的分配满足该段全部观测的桶计数与总数；
2. 分配只使用值域内整数，重数非负；
3. 分配合并样本的第 k 小值恰好等于见证声明的边界值；
4. 用内嵌观测从零重建约束模型，重新计算的极值与声明值一致；
5. 见证分配确实是对应极值解（最小见证用最大前缀和解、
   最大见证用最小前缀和解，逐单元比对）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .model import (
    Allocation,
    SegmentModel,
    Witness,
    WitnessEntry,
)
from .records import Observation


@dataclass
class VerificationReport:
    ok: bool
    errors: List[str] = field(default_factory=list)
    checks_passed: List[str] = field(default_factory=list)

    def raise_on_failure(self) -> None:
        if not self.ok:
            raise AssertionError("见证验算失败: " + "; ".join(self.errors))


def _allocation_multiplicity(alloc: Allocation, v: int) -> int:
    total = 0
    for r in alloc.rle:
        if r.cell_lo <= v <= r.cell_hi:
            total += r.count
    return total


def _bucket_overlap_count(alloc: Allocation, lo: int, hi: int) -> int:
    """分配落在闭区间 [lo, hi] 内的样本数。"""
    total = 0
    for r in alloc.rle:
        a = max(r.cell_lo, lo)
        b = min(r.cell_hi, hi)
        if a <= b:
            total += r.count * (b - a + 1)
    return total


def _kth_of_allocation(alloc: Allocation, k: int) -> int:
    points = sorted(
        (r.cell_lo, r.cell_hi, r.count) for r in alloc.rle if r.count > 0
    )
    cum = 0
    for lo, hi, count in points:
        width = hi - lo + 1
        cum += count * width
        if cum >= k:
            # 该 RLE 段内每个整数点重数均为 count
            offset = (k - (cum - count * width) - 1) // count
            return lo + offset
    raise ValueError("分配中的样本少于 k")


def _as_witness_entries(alloc: Allocation, bound: str) -> List[WitnessEntry]:
    """RLE 极值分配落实到单元边界点后的见证条目。"""
    entries = []
    for r in alloc.rle:
        v = r.cell_lo if bound == "min" else r.cell_hi
        entries.append(WitnessEntry(v, v, r.count))
    return entries


def verify_witness(
    witness: Witness, expected_store_version: Optional[int] = None
) -> VerificationReport:
    """独立验算一份见证，返回结构化报告。"""
    report = VerificationReport(ok=True)

    def fail(msg: str) -> None:
        report.ok = False
        report.errors.append(msg)

    if witness.bound not in ("min", "max"):
        fail(f"bound 必须是 min/max，实际为 {witness.bound}")
        return report
    if expected_store_version is not None and (
        witness.store_version != expected_store_version
    ):
        fail(
            f"见证版本 {witness.store_version} 与期望版本 "
            f"{expected_store_version} 不一致"
        )

    # 按段分组内嵌观测
    by_segment: Dict[Tuple[str, int], List[Observation]] = {}
    for obs in witness.observations:
        by_segment.setdefault(
            (obs.source, obs.segment_start), []
        ).append(obs)

    alloc_by_segment: Dict[Tuple[str, int], Allocation] = {
        (a.source, a.segment_start): a for a in witness.allocations
    }
    if set(by_segment) != set(alloc_by_segment):
        fail(
            f"见证覆盖的段不一致: 观测段 {sorted(by_segment)} "
            f"分配段 {sorted(alloc_by_segment)}"
        )
        return report

    # 逐段检查桶约束、值域，并重建模型
    rebuilt_models: List[SegmentModel] = []
    for seg_key, obs_list in by_segment.items():
        alloc = alloc_by_segment[seg_key]
        domain_lo = min(o.lo for o in obs_list)
        domain_hi = max(o.hi for o in obs_list)
        # 值域一致性由 SegmentModel 校验，这里先检查分配位置
        for r in alloc.rle:
            if r.cell_lo < domain_lo or r.cell_hi > domain_hi:
                fail(
                    f"段 {seg_key} 分配越界: RLE[{r.cell_lo},{r.cell_hi}] "
                    f"超出值域 [{domain_lo},{domain_hi}]"
                )
            if r.count < 0:
                fail(f"段 {seg_key} 存在负重数 {r.count}")
        try:
            model = SegmentModel(obs_list)
        except Exception as exc:  # noqa: BLE001 - 验算需捕获全部异常
            fail(f"段 {seg_key} 内嵌观测无法建模: {exc}")
            continue
        if not model.feasible:
            fail(f"段 {seg_key} 内嵌观测本身不可行: {model.reason}")
            continue
        rebuilt_models.append(model)

        if alloc.total() != model.total:
            fail(
                f"段 {seg_key} 分配样本数 {alloc.total()} "
                f"!= 约束总数 {model.total}"
            )
        for obs in obs_list:
            for b in obs.buckets:
                got = _bucket_overlap_count(alloc, b.lo, b.hi)
                if got != b.count:
                    fail(
                        f"段 {seg_key} 系列 {obs.series_id} rev "
                        f"{obs.revision} 桶 [{b.lo},{b.hi}] 计数 "
                        f"{got} != 声明 {b.count}"
                    )

        # 极值解一致性：分配必须恰好是声明边界对应的极值单元计数
        expected_counts = model.cell_counts(maximize=(witness.bound == "min"))
        actual_counts = []
        for i in range(len(model.points) - 1):
            cell_lo = model.points[i] + 1
            cell_hi = model.points[i + 1]
            actual_counts.append(
                _bucket_overlap_count(alloc, cell_lo, cell_hi)
            )
        if actual_counts != expected_counts:
            fail(
                f"段 {seg_key} 分配不是 {witness.bound} 边界的极值解: "
                f"实际 {actual_counts} 期望 {expected_counts}"
            )

    if not report.ok:
        return report
    report.checks_passed.append("桶计数/总数/值域/非负约束全部满足")
    report.checks_passed.append("每段分配与极值解单元计数一致")

    # 合并分配的第 k 小值检查
    k = witness.k
    entries: List[WitnessEntry] = []
    for model in rebuilt_models:
        alloc = alloc_by_segment[(model.source, model.segment_start)]
        entries.extend(_as_witness_entries(alloc, witness.bound))
    entries.sort(key=lambda e: e.lo)
    cum = 0
    merged_kth = None
    for e in entries:
        cum += e.total
        if cum >= k:
            merged_kth = e.lo
            break
    if merged_kth is None:
        fail(f"合并分配样本数不足 k={k}")
        return report
    if merged_kth != witness.kth_value:
        fail(
            f"合并分配第 {k} 小值为 {merged_kth}，"
            f"见证声明 {witness.kth_value}"
        )
        return report
    report.checks_passed.append(f"合并第 {k} 小值 = {merged_kth}")

    # 从零重算极值并比对
    from .window import compute_window_bounds

    try:
        rec_min, rec_max = compute_window_bounds(rebuilt_models, k)
    except Exception as exc:  # noqa: BLE001
        fail(f"重建模型后极值计算失败: {exc}")
        return report
    declared = witness.kth_value
    if witness.bound == "min" and declared != rec_min:
        fail(f"重算最小 kth={rec_min}，见证声明 {declared}")
    if witness.bound == "max" and declared != rec_max:
        fail(f"重算最大 kth={rec_max}，见证声明 {declared}")
    report.checks_passed.append(
        f"独立重算边界 min={rec_min} max={rec_max}，与声明一致"
    )
    return report
