"""治理裁决的数据模型。

模型采用「风险段（segment）」作为最小扣减单位：

* 每个资产由一个或多个风险段组成，风险段给出该段的初始风险区间；
  各段下界之和、上界之和应当等于资产声明的影响区间。
* 控制项（Control）声明自己覆盖哪些风险段，以及在每个段上的
  固定消减量（非负标量，沿区间两端等幅扣减）。
* 控制项通过 ``common_cause_group`` 声明共因关系：取值相同的控制
  属于同一共因组，``None`` 表示该控制自成独立组。
* 验证回执（ValidationReceipt）决定控制项是否有效，只有
  ``status == "valid"`` 的回执才允许对应控制参与扣减。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# 输入模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """资产下的一个风险段，携带初始风险区间 [low, high]。"""

    segment_id: str
    asset_id: str
    low: float
    high: float

    def __post_init__(self) -> None:
        if not isinstance(self.segment_id, str) or not self.segment_id:
            raise ValueError("segment_id 必须是非空字符串")
        if not isinstance(self.asset_id, str) or not self.asset_id:
            raise ValueError("asset_id 必须是非空字符串")
        if self.low < 0 or self.high < 0:
            raise ValueError(f"风险段 {self.segment_id} 的风险值不能为负")
        if self.low > self.high:
            raise ValueError(
                f"风险段 {self.segment_id} 的下界 {self.low} 大于上界 {self.high}"
            )


@dataclass(frozen=True)
class Asset:
    """上线申请中的受影响资产。"""

    asset_id: str
    low: float
    high: float
    required: bool = True
    go_live_limit: Optional[float] = None

    def __post_init__(self) -> None:
        if not isinstance(self.asset_id, str) or not self.asset_id:
            raise ValueError("asset_id 必须是非空字符串")
        if self.low < 0 or self.high < 0:
            raise ValueError(f"资产 {self.asset_id} 的影响值不能为负")
        if self.low > self.high:
            raise ValueError(
                f"资产 {self.asset_id} 的影响下界 {self.low} 大于上界 {self.high}"
            )
        if self.go_live_limit is not None and self.go_live_limit < 0:
            raise ValueError(
                f"资产 {self.asset_id} 的上线限额不能为负"
            )


@dataclass(frozen=True)
class Control:
    """防护控制项及其消减范围。

    ``coverage`` 为 ``{segment_id: 消减量}`` 映射；同一控制可覆盖
    多个风险段，每个段上的消减量独立声明。
    ``common_cause_group`` 相同的控制互为共因控制；``None`` 表示
    该控制不与任何其他控制共享共因。
    """

    control_id: str
    coverage: dict[str, float]
    common_cause_group: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.control_id, str) or not self.control_id:
            raise ValueError("control_id 必须是非空字符串")
        if not isinstance(self.coverage, dict) or not self.coverage:
            raise ValueError(f"控制 {self.control_id} 必须声明非空 coverage")
        for seg_id, amount in self.coverage.items():
            if not isinstance(seg_id, str) or not seg_id:
                raise ValueError(
                    f"控制 {self.control_id} 的 coverage 存在非法风险段标识"
                )
            if amount < 0:
                raise ValueError(
                    f"控制 {self.control_id} 在风险段 {seg_id} 上的消减量不能为负"
                )


@dataclass(frozen=True)
class ValidationReceipt:
    """控制项的验证回执。

    只有 ``status == "valid"`` 的回执才允许对应控制参与风险扣减；
    其余状态（expired / revoked / failed / 未知状态）一律视为无效。
    """

    receipt_id: str
    control_id: str
    status: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.receipt_id, str) or not self.receipt_id:
            raise ValueError("receipt_id 必须是非空字符串")
        if not isinstance(self.control_id, str) or not self.control_id:
            raise ValueError("回执的 control_id 必须是非空字符串")
        if not isinstance(self.status, str) or not self.status:
            raise ValueError(f"回执 {self.receipt_id} 的 status 必须是非空字符串")

    @property
    def is_valid(self) -> bool:
        return self.status == "valid"


# ---------------------------------------------------------------------------
# 输出模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupContribution:
    """单个共因组（或独立控制）在某个风险段上的实际扣减贡献。

    共因组内多个控制在同一段重叠时，只按有效的最高消减量计算，
    ``selected_control_id`` 记录被选中的最高消减量控制；
    消减量相等时取标识字典序最小者，保证结果稳定可复现。
    """

    group_key: str
    deducted: float
    selected_control_id: str
    candidate_control_ids: tuple[str, ...]
    candidate_mitigations: dict[str, float]


@dataclass(frozen=True)
class SegmentResult:
    """单个风险段的扣减明细与剩余风险区间。"""

    segment_id: str
    initial_low: float
    initial_high: float
    deducted: float
    residual_low: float
    residual_high: float
    contributions: tuple[GroupContribution, ...]
    covered_control_ids: tuple[str, ...]
    capped: bool


@dataclass(frozen=True)
class ControlOutcome:
    """控制项在裁决中的处理结果，作为「仍依赖的控制依据」留痕。"""

    control_id: str
    valid: bool
    reason: str
    covered_segment_ids: tuple[str, ...]
    common_cause_group: Optional[str]


@dataclass(frozen=True)
class Breach:
    """必需资产剩余风险超出上线限额的违约记录。"""

    asset_id: str
    limit: float
    residual_low: float
    residual_high: float
    excess: float
    relied_control_ids: tuple[str, ...]
    uncovered_segment_ids: tuple[str, ...]


@dataclass(frozen=True)
class AssetResult:
    """单个资产的剩余风险区间与扣减明细。"""

    asset_id: str
    required: bool
    initial_low: float
    initial_high: float
    residual_low: float
    residual_high: float
    go_live_limit: Optional[float]
    within_limit: Optional[bool]
    segment_results: tuple[SegmentResult, ...]
    relied_control_ids: tuple[str, ...]
    uncovered_segment_ids: tuple[str, ...]


@dataclass(frozen=True)
class GovernanceDecision:
    """完整治理裁决结果。

    ``decision`` 取值：
      * ``"proceed"``  —— 所有必需资产剩余风险均在限额内；
      * ``"suspended"`` —— 至少一项必需资产剩余风险上界超出限额。
    """

    decision: str
    asset_results: tuple[AssetResult, ...]
    breaches: tuple[Breach, ...]
    control_outcomes: tuple[ControlOutcome, ...]
    default_go_live_limit: Optional[float]

    def to_dict(self) -> dict:
        """转为可 JSON 序列化的普通字典（元组转为列表）。"""
        import dataclasses

        def _convert(obj: object) -> object:
            if dataclasses.is_dataclass(obj):
                return {
                    key: _convert(value)
                    for key, value in dataclasses.asdict(obj).items()
                }
            if isinstance(obj, (list, tuple)):
                return [_convert(item) for item in obj]
            if isinstance(obj, dict):
                return {key: _convert(value) for key, value in obj.items()}
            return obj

        return _convert(self)  # type: ignore[return-value]
