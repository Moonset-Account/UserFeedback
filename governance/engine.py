"""治理裁决核心引擎。

扣减规则（题面约束的直接实现）：

1. 只有持有有效验证回执（``status == "valid"``）的控制才参与扣减；
2. 同一共因组的多个有效控制在同一风险段上的重叠覆盖，只按
   **有效的最高消减量** 计算一次，不重复扣减；
3. 不同共因组（含独立控制，各自成组）在同一段上的扣减量相加；
4. 单段总扣减最多扣完该段初始风险（对区间两端分别封顶到
   ``initial_low`` / ``initial_high``），剩余风险不为负；
5. 任一必需资产的剩余风险 **上界** 超出其上线限额时，裁决暂停，
   并给出剩余风险区间与仍依赖的控制依据。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Optional

from governance.errors import GovernanceInputError
from governance.model import (
    Asset,
    AssetResult,
    Breach,
    Control,
    ControlOutcome,
    GovernanceDecision,
    GroupContribution,
    Segment,
    SegmentResult,
    ValidationReceipt,
)

# 浮点比较容差：封顶、限额比较、区间对账均使用该容差，
# 吸收二进制浮点累加误差。
EPS = 1e-9

# 有效回执状态白名单，其余状态一律视为无效证据。
VALID_RECEIPT_STATUS = "valid"


def _group_key(control: Control) -> str:
    """控制的共因分组键；未声明共因组的控制各自独立成组。"""
    if control.common_cause_group is None:
        return f"independent::{control.control_id}"
    return f"common-cause::{control.common_cause_group}"


def _validate_inputs(
    assets: Iterable[Asset],
    segments: Iterable[Segment],
    controls: Iterable[Control],
    receipts: Iterable[ValidationReceipt],
    default_go_live_limit: Optional[float],
) -> tuple[
    dict[str, Asset],
    dict[str, Segment],
    dict[str, Control],
    dict[str, ValidationReceipt],
]:
    """结构与交叉约束校验，失败时抛出 :class:`GovernanceInputError`。"""
    assets = list(assets)
    segments = list(segments)
    controls = list(controls)
    receipts = list(receipts)

    if not assets:
        raise GovernanceInputError("至少需要一个资产")
    if not segments:
        raise GovernanceInputError("至少需要一个风险段")
    if default_go_live_limit is not None and default_go_live_limit < 0:
        raise GovernanceInputError("默认上线限额不能为负")

    # --- 资产 -------------------------------------------------------------
    asset_map: dict[str, Asset] = {}
    for asset in assets:
        if asset.asset_id in asset_map:
            raise GovernanceInputError(f"资产标识重复: {asset.asset_id}")
        asset_map[asset.asset_id] = asset

    # --- 风险段 -----------------------------------------------------------
    segment_map: dict[str, Segment] = {}
    segment_sums: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for segment in segments:
        if segment.segment_id in segment_map:
            raise GovernanceInputError(f"风险段标识重复: {segment.segment_id}")
        if segment.asset_id not in asset_map:
            raise GovernanceInputError(
                f"风险段 {segment.segment_id} 引用了不存在的资产 "
                f"{segment.asset_id}"
            )
        segment_map[segment.segment_id] = segment
        segment_sums[segment.asset_id][0] += segment.low
        segment_sums[segment.asset_id][1] += segment.high

    for asset_id, asset in asset_map.items():
        if asset_id not in segment_sums:
            raise GovernanceInputError(
                f"资产 {asset_id} 必须至少声明一个风险段"
            )
        sum_low, sum_high = segment_sums[asset_id]
        if abs(sum_low - asset.low) > EPS or abs(sum_high - asset.high) > EPS:
            raise GovernanceInputError(
                f"资产 {asset_id} 的风险段区间之和 [{sum_low}, {sum_high}] "
                f"与声明影响区间 [{asset.low}, {asset.high}] 不一致"
            )

    # --- 控制项 -----------------------------------------------------------
    control_map: dict[str, Control] = {}
    for control in controls:
        if control.control_id in control_map:
            raise GovernanceInputError(f"控制项标识重复: {control.control_id}")
        for segment_id in control.coverage:
            if segment_id not in segment_map:
                raise GovernanceInputError(
                    f"控制 {control.control_id} 覆盖了不存在的风险段 "
                    f"{segment_id}"
                )
            owner = segment_map[segment_id].asset_id
            for other_id in control.coverage:
                other_owner = segment_map[other_id].asset_id
                if other_owner != owner:
                    raise GovernanceInputError(
                        f"控制 {control.control_id} 不能跨资产覆盖风险段: "
                        f"{segment_id} 属于 {owner}，{other_id} 属于 {other_owner}"
                    )
        control_map[control.control_id] = control

    # --- 验证回执 ---------------------------------------------------------
    receipt_map: dict[str, ValidationReceipt] = {}
    for receipt in receipts:
        if receipt.receipt_id in receipt_map:
            raise GovernanceInputError(f"回执标识重复: {receipt.receipt_id}")
        if receipt.control_id not in control_map:
            raise GovernanceInputError(
                f"回执 {receipt.receipt_id} 引用了不存在的控制项 "
                f"{receipt.control_id}"
            )
        receipt_map[receipt.receipt_id] = receipt

    # 每个控制最多绑定一张回执（多份证据绑定同一控制时必须显式拒绝，
    # 避免静默挑选其中一张）。
    controls_with_receipt: set[str] = set()
    for receipt in receipts:
        if receipt.control_id in controls_with_receipt:
            raise GovernanceInputError(
                f"控制项 {receipt.control_id} 绑定了多张验证回执，"
                "每个控制项至多允许一张回执"
            )
        controls_with_receipt.add(receipt.control_id)

    return asset_map, segment_map, control_map, receipt_map


def _resolve_control_validity(
    controls: dict[str, Control],
    receipts: dict[str, ValidationReceipt],
) -> dict[str, ControlOutcome]:
    """汇总每个控制项是否有效及原因。"""
    receipt_by_control = {
        receipt.control_id: receipt for receipt in receipts.values()
    }
    outcomes: dict[str, ControlOutcome] = {}
    for control_id, control in controls.items():
        receipt = receipt_by_control.get(control_id)
        covered = tuple(sorted(control.coverage))
        if receipt is None:
            outcomes[control_id] = ControlOutcome(
                control_id=control_id,
                valid=False,
                reason="缺少验证回执，证据不被采信",
                covered_segment_ids=covered,
                common_cause_group=control.common_cause_group,
            )
        elif receipt.is_valid:
            outcomes[control_id] = ControlOutcome(
                control_id=control_id,
                valid=True,
                reason=f"验证回执 {receipt.receipt_id} 有效"
                + (f"（{receipt.detail}）" if receipt.detail else ""),
                covered_segment_ids=covered,
                common_cause_group=control.common_cause_group,
            )
        else:
            outcomes[control_id] = ControlOutcome(
                control_id=control_id,
                valid=False,
                reason=(
                    f"验证回执 {receipt.receipt_id} 状态为 {receipt.status}，"
                    "证据无效"
                    + (f"（{receipt.detail}）" if receipt.detail else "")
                ),
                covered_segment_ids=covered,
                common_cause_group=control.common_cause_group,
            )
    return outcomes


def _evaluate_segment(
    segment: Segment,
    controls: dict[str, Control],
    valid_control_ids: set[str],
) -> SegmentResult:
    """计算单个风险段上的共因去重扣减与剩余风险区间。"""
    # group_key -> {control_id: 在该段上的消减量}
    group_candidates: dict[str, dict[str, float]] = defaultdict(dict)
    covered: set[str] = set()

    for control_id in sorted(valid_control_ids):
        control = controls[control_id]
        mitigation = control.coverage.get(segment.segment_id)
        if mitigation is None:
            continue
        covered.add(control_id)
        group_candidates[_group_key(control)][control_id] = mitigation

    contributions: list[GroupContribution] = []
    for group_key in sorted(group_candidates):
        candidates = group_candidates[group_key]
        # 共因组重叠范围只按有效的最高消减量计算；
        # 持平取控制标识字典序最小者，保证裁决稳定可复现。
        selected_control_id = min(
            candidates,
            key=lambda cid: (-candidates[cid], cid),
        )
        contributions.append(
            GroupContribution(
                group_key=group_key,
                deducted=candidates[selected_control_id],
                selected_control_id=selected_control_id,
                candidate_control_ids=tuple(sorted(candidates)),
                candidate_mitigations=dict(sorted(candidates.items())),
            )
        )

    raw_deducted = sum(item.deducted for item in contributions)
    # 独立组相加后最多扣完该段初始风险：区间两端分别封顶。
    capped_low = min(raw_deducted, segment.low)
    capped_high = min(raw_deducted, segment.high)
    capped = (
        raw_deducted > segment.low + EPS or raw_deducted > segment.high + EPS
    )

    residual_low = segment.low - capped_low
    residual_high = segment.high - capped_high
    # 浮点误差兜底，确保剩余风险非负。
    if 0 > residual_low >= -EPS:
        residual_low = 0.0
    if 0 > residual_high >= -EPS:
        residual_high = 0.0

    return SegmentResult(
        segment_id=segment.segment_id,
        initial_low=segment.low,
        initial_high=segment.high,
        deducted=min(raw_deducted, segment.high),
        residual_low=residual_low,
        residual_high=residual_high,
        contributions=tuple(contributions),
        covered_control_ids=tuple(sorted(covered)),
        capped=capped,
    )


def evaluate_application(
    assets: Iterable[Asset],
    segments: Iterable[Segment],
    controls: Iterable[Control],
    receipts: Iterable[ValidationReceipt],
    default_go_live_limit: Optional[float] = None,
) -> GovernanceDecision:
    """执行上线申请的治理裁决。

    参数:
        assets: 受影响资产及其影响区间、必需性与（可选的）逐资产限额。
        segments: 资产下的风险段，各段区间之和须等于资产影响区间。
        controls: 控制项，声明覆盖的风险段、消减量与共因组。
        receipts: 控制项验证回执，决定控制是否有效。
        default_go_live_limit: 必需资产未单独声明限额时使用的默认限额；
            若两者都缺失则抛出 :class:`GovernanceInputError`。

    返回:
        :class:`GovernanceDecision`，含裁决结论、逐资产剩余风险区间、
        违约记录与控制依据留痕。
    """
    asset_map, segment_map, control_map, receipt_map = _validate_inputs(
        assets, segments, controls, receipts, default_go_live_limit
    )
    control_outcomes = _resolve_control_validity(control_map, receipt_map)
    valid_control_ids = {
        control_id
        for control_id, outcome in control_outcomes.items()
        if outcome.valid
    }

    # asset_id -> 该资产下的风险段结果
    segments_by_asset: dict[str, list[SegmentResult]] = defaultdict(list)
    for segment in sorted(segment_map.values(), key=lambda s: s.segment_id):
        segment_result = _evaluate_segment(
            segment, control_map, valid_control_ids
        )
        segments_by_asset[segment.asset_id].append(segment_result)

    asset_results: list[AssetResult] = []
    breaches: list[Breach] = []

    for asset_id in sorted(asset_map):
        asset = asset_map[asset_id]
        seg_results = segments_by_asset[asset_id]

        residual_low = sum(item.residual_low for item in seg_results)
        residual_high = sum(item.residual_high for item in seg_results)
        if -EPS <= residual_low <= 0:
            residual_low = 0.0
        if -EPS <= residual_high <= 0:
            residual_high = 0.0

        relied = sorted(
            {
                control_id
                for item in seg_results
                for control_id in item.covered_control_ids
            }
        )
        uncovered = sorted(
            item.segment_id
            for item in seg_results
            if not item.covered_control_ids
        )

        within_limit: Optional[bool]
        limit: Optional[float]
        if not asset.required:
            within_limit = None
            limit = asset.go_live_limit
        else:
            limit = asset.go_live_limit
            if limit is None:
                limit = default_go_live_limit
            if limit is None:
                raise GovernanceInputError(
                    f"必需资产 {asset_id} 未声明上线限额，且未提供默认限额"
                )
            # 保守口径：以上界（最坏情况）与限额比较。
            within_limit = residual_high <= limit + EPS

            if not within_limit:
                breaches.append(
                    Breach(
                        asset_id=asset_id,
                        limit=limit,
                        residual_low=residual_low,
                        residual_high=residual_high,
                        excess=max(0.0, residual_high - limit),
                        relied_control_ids=tuple(relied),
                        uncovered_segment_ids=tuple(uncovered),
                    )
                )

        asset_results.append(
            AssetResult(
                asset_id=asset_id,
                required=asset.required,
                initial_low=asset.low,
                initial_high=asset.high,
                residual_low=residual_low,
                residual_high=residual_high,
                go_live_limit=limit,
                within_limit=within_limit,
                segment_results=tuple(seg_results),
                relied_control_ids=tuple(relied),
                uncovered_segment_ids=tuple(uncovered),
            )
        )

    decision = "suspended" if breaches else "proceed"
    return GovernanceDecision(
        decision=decision,
        asset_results=tuple(asset_results),
        breaches=tuple(breaches),
        control_outcomes=tuple(
            control_outcomes[control_id]
            for control_id in sorted(control_outcomes)
        ),
        default_go_live_limit=default_go_live_limit,
    )
