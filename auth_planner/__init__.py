"""认证规划组件（auth_planner）。

为付款部门的敏感操作选择认证因素组合：
- 覆盖操作声明的认证能力；
- 满足因素间相互独立条件（不同物理设备且不共享恢复渠道）；
- 在所有可行方案中选择用户操作步骤最少的一组；
- 无可行方案时给出缺少的独立能力；
- 因素停用后可重新规划。
"""

from .models import (
    CAPABILITY_FIDO2,
    CAPABILITY_HARDWARE_OTP,
    CAPABILITY_MOBILE_PUSH,
    CAPABILITY_PASSWORD,
    CAPABILITY_SMS_OTP,
    CAPABILITY_TOTP,
    AuthFactor,
    OperationRequirement,
    PlanResult,
    MissingCapability,
)
from .planner import AuthenticationPlanner

__all__ = [
    "AuthFactor",
    "OperationRequirement",
    "PlanResult",
    "MissingCapability",
    "AuthenticationPlanner",
    "CAPABILITY_PASSWORD",
    "CAPABILITY_TOTP",
    "CAPABILITY_SMS_OTP",
    "CAPABILITY_FIDO2",
    "CAPABILITY_HARDWARE_OTP",
    "CAPABILITY_MOBILE_PUSH",
]
