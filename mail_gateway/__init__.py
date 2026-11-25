"""邮件加密网关：投递包编排的防御性安全规划。

公开 API
--------
- :class:`Key` / :class:`Recipient`：输入模型。
- :class:`PackageOption`：单个收件人子集的包评估结果。
- :class:`PlannedPackage` / :class:`DeliveryPlan`：拆投方案模型。
- :func:`evaluate_package`：评估一个子集能否成包，并给最少封套方案。
- :func:`plan_delivery`：求包数最少、同包数下封套数最少的安全拆投。
- :class:`InfeasibleDeliveryError`：不存在安全拆投时抛出。
"""

from .model import Key, PackageOption, Recipient, evaluate_package
from .planner import (
    MAX_RECIPIENTS,
    DeliveryPlan,
    InfeasibleDeliveryError,
    PlannedPackage,
    plan_delivery,
)

__all__ = [
    "Key",
    "Recipient",
    "PackageOption",
    "PlannedPackage",
    "DeliveryPlan",
    "evaluate_package",
    "plan_delivery",
    "InfeasibleDeliveryError",
    "MAX_RECIPIENTS",
]
