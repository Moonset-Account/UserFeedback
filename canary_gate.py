"""灰度闸门（canary gate）：按地区权重比较新版与基线错误率的发布裁决。

裁决规则（对每批回执处理后计算）：

- 设每个地区 r 的权重为 w_r（正有理数，总和为 1），阈值为 t（有理数）。
- 每条已冻结请求最终只会是成功或失败；未回报的请求两种结果都可能。
- 地区差值 d_r = 新版错误率 - 基线错误率，加权总和 S = Σ w_r * d_r。
- 在所有未回报请求的所有可能取值下，S 可取得下界 lower 与上界 upper：
    - lower：新版未回报全部成功、基线未回报全部失败；
    - upper：新版未回报全部失败、基线未回报全部成功。
- lower > t  → 回滚（ROLLBACK），记录触发批次与界值 lower；
- upper <= t → 放量（RAMP_UP），记录触发批次与界值 upper；
- 否则继续等待（PENDING）。

所有算术使用 fractions.Fraction 精确进行，不存在浮点误差。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from typing import Dict, Iterable, Mapping, Optional, Set, Tuple


class Decision(Enum):
    """闸门裁决结果。"""

    PENDING = "pending"
    ROLLBACK = "rollback"
    RAMP_UP = "ramp_up"


class GateClosedError(RuntimeError):
    """闸门已给出终局裁决后仍继续提交回执批次时抛出。"""


class UnknownRequestError(ValueError):
    """回执中的请求不在冻结的分组内时抛出。"""


@dataclass(frozen=True)
class Receipt:
    """一条回执：某请求的最终结果。success=True 表示成功，False 表示失败。"""

    request_id: str
    success: bool


@dataclass(frozen=True)
class RegionGroups:
    """一个地区冻结的分组：新版组与基线组的请求 ID 集合。

    两组都必须非空且互不重叠。
    """

    new: frozenset
    baseline: frozenset

    def __init__(self, new: Iterable[str], baseline: Iterable[str]) -> None:
        new_set = frozenset(new)
        baseline_set = frozenset(baseline)
        if not new_set:
            raise ValueError("新版组不能为空")
        if not baseline_set:
            raise ValueError("基线组不能为空")
        overlap = new_set & baseline_set
        if overlap:
            raise ValueError(f"新版组与基线组存在重叠请求: {sorted(overlap)!r}")
        object.__setattr__(self, "new", new_set)
        object.__setattr__(self, "baseline", baseline_set)


@dataclass(frozen=True)
class GateReport:
    """一批回执处理后的闸门报告。

    - decision: 当前裁决。
    - batch_index: 本报告对应的批次号（从 1 开始）。
    - lower / upper: 处理后加权差值可取得的下界 / 上界。
    - triggered_bound: 终局裁决时触发裁决的界值（回滚为 lower，放量为 upper），
      未决时为 None。
    """

    decision: Decision
    batch_index: int
    lower: Fraction
    upper: Fraction
    triggered_bound: Optional[Fraction]


@dataclass
class _RegionState:
    new_ids: frozenset
    baseline_ids: frozenset
    new_fail: int = 0
    new_success: int = 0
    baseline_fail: int = 0
    baseline_success: int = 0


class CanaryGate:
    """灰度闸门。

    参数：
        groups: 地区名 -> RegionGroups（或 (new_ids, baseline_ids) 二元组）。
        weights: 地区名 -> 权重，键集合必须与 groups 完全一致；
            每个权重为正有理数，总和精确等于 1。
        threshold: 阈值，有理数。

    权重、阈值接受 Fraction / int / 有限小数字符串等 Fraction 可精确转换的值。
    """

    def __init__(
        self,
        groups: Mapping[str, object],
        weights: Mapping[str, object],
        threshold: object,
    ) -> None:
        if not groups:
            raise ValueError("至少需要一个地区")
        region_names = set(groups)
        weight_names = set(weights)
        if region_names != weight_names:
            raise ValueError(
                f"权重键集合必须与地区一致: "
                f"缺少 {sorted(region_names - weight_names)!r}, "
                f"多余 {sorted(weight_names - region_names)!r}"
            )

        parsed_weights: Dict[str, Fraction] = {}
        total = Fraction(0)
        for name, raw in weights.items():
            w = self._as_fraction(raw, f"权重[{name!r}]")
            if w <= 0:
                raise ValueError(f"权重[{name!r}]必须为正, 实际为 {w}")
            parsed_weights[name] = w
            total += w
        if total != 1:
            raise ValueError(f"权重总和必须为 1, 实际为 {total}")

        self._threshold = self._as_fraction(threshold, "阈值")
        self._weights = parsed_weights

        self._regions: Dict[str, _RegionState] = {}
        self._owner: Dict[str, Tuple[str, str]] = {}  # request_id -> (region, "new"/"baseline")
        for name, spec in groups.items():
            rg = spec if isinstance(spec, RegionGroups) else RegionGroups(*spec)
            state = _RegionState(new_ids=rg.new, baseline_ids=rg.baseline)
            self._regions[name] = state
            for rid in rg.new:
                if rid in self._owner:
                    raise ValueError(f"请求 {rid!r} 在多个地区/分组中重复冻结")
                self._owner[rid] = (name, "new")
            for rid in rg.baseline:
                if rid in self._owner:
                    raise ValueError(f"请求 {rid!r} 在多个地区/分组中重复冻结")
                self._owner[rid] = (name, "baseline")

        self._reported: Set[str] = set()
        self._batch_count = 0
        self._final_report: Optional[GateReport] = None

    # ------------------------------------------------------------------ API

    @property
    def threshold(self) -> Fraction:
        return self._threshold

    @property
    def weights(self) -> Mapping[str, Fraction]:
        return dict(self._weights)

    @property
    def final_report(self) -> Optional[GateReport]:
        """首次可判时记录的终局报告；未决时为 None。"""
        return self._final_report

    def bounds(self) -> Tuple[Fraction, Fraction]:
        """当前状态下加权差值（新版错误率 - 基线错误率）的下界与上界。"""
        lower = Fraction(0)
        upper = Fraction(0)
        for name, st in self._regions.items():
            w = self._weights[name]
            n_new = len(st.new_ids)
            n_base = len(st.baseline_ids)
            unreported_new = n_new - st.new_fail - st.new_success
            unreported_base = n_base - st.baseline_fail - st.baseline_success
            lo = Fraction(st.new_fail, n_new) - Fraction(
                st.baseline_fail + unreported_base, n_base
            )
            hi = Fraction(st.new_fail + unreported_new, n_new) - Fraction(
                st.baseline_fail, n_base
            )
            lower += w * lo
            upper += w * hi
        return lower, upper

    def submit_batch(
        self, receipts: Iterable[object]
    ) -> GateReport:
        """提交一批回执并返回处理后的报告。

        - 批次号从 1 开始按提交顺序递增。
        - 重复回执（同一请求已回报过）只计一次，后续忽略。
        - 未冻结的请求 ID 抛出 UnknownRequestError（整批作废，状态不变）。
        - 首次可判时记录触发批次与界值；此后闸门关闭，
          再提交批次抛出 GateClosedError。
        """
        if self._final_report is not None:
            raise GateClosedError(
                f"闸门已于第 {self._final_report.batch_index} 批给出裁决 "
                f"{self._final_report.decision.value}，不再接受回执"
            )

        parsed = [self._parse_receipt(r) for r in receipts]
        # 先校验整批，避免部分应用。
        for rid, _ in parsed:
            if rid not in self._owner and rid not in self._reported:
                raise UnknownRequestError(f"请求 {rid!r} 不在冻结的分组内")

        self._batch_count += 1
        for rid, success in parsed:
            if rid in self._reported:
                continue  # 重复回执只计一次
            self._reported.add(rid)
            region, arm = self._owner[rid]
            st = self._regions[region]
            if arm == "new":
                if success:
                    st.new_success += 1
                else:
                    st.new_fail += 1
            else:
                if success:
                    st.baseline_success += 1
                else:
                    st.baseline_fail += 1

        lower, upper = self.bounds()
        if lower > self._threshold:
            report = GateReport(
                Decision.ROLLBACK, self._batch_count, lower, upper, lower
            )
            self._final_report = report
        elif upper <= self._threshold:
            report = GateReport(
                Decision.RAMP_UP, self._batch_count, lower, upper, upper
            )
            self._final_report = report
        else:
            report = GateReport(
                Decision.PENDING, self._batch_count, lower, upper, None
            )
        return report

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _as_fraction(value: object, label: str) -> Fraction:
        try:
            return Fraction(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}必须是有理数: {value!r}") from exc

    @staticmethod
    def _parse_receipt(receipt: object) -> Tuple[str, bool]:
        if isinstance(receipt, Receipt):
            return receipt.request_id, bool(receipt.success)
        rid, success = receipt  # type: ignore[misc]
        return str(rid), bool(success)
