"""上线防护证据治理裁决模块。

公共入口：
    evaluate_application       —— 核心裁决 API
    GovernanceInputError       —— 输入校验错误
    load_application_payload   —— 从 JSON 结构加载裁决输入
"""

from governance.engine import evaluate_application
from governance.errors import GovernanceInputError
from governance.jsonio import load_application_payload
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

__all__ = [
    "evaluate_application",
    "GovernanceInputError",
    "load_application_payload",
    "Asset",
    "AssetResult",
    "Breach",
    "Control",
    "ControlOutcome",
    "GovernanceDecision",
    "GroupContribution",
    "Segment",
    "SegmentResult",
    "ValidationReceipt",
]
