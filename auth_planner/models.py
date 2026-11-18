"""认证规划领域模型。

独立性规则（题面交叉约束的具体化）：
- 两个因素若由同一物理设备（``device_id`` 相同）提供，则它们不是相互独立的
  证明；它们可以同时出现在方案中（分别覆盖不同能力），但在"独立证明"计数时
  只算一个独立来源。
- 两个因素若绑定同一恢复渠道（``recovery_channel_id`` 相同），同样不相互独立
  —— 恢复/重置路径重合意味着一个渠道失陷会连带另一个。
- 因此，选中因素按"同设备或同恢复渠道"连边构成冲突图，图的每个连通分量代表
  一个独立来源，连通分量数即该方案提供的独立证明数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet

# 常见认证能力常量；规划器本身不限制取值，调用方也可使用自定义字符串。
CAPABILITY_PASSWORD = "password"
CAPABILITY_TOTP = "totp"
CAPABILITY_SMS_OTP = "sms_otp"
CAPABILITY_FIDO2 = "fido2"
CAPABILITY_HARDWARE_OTP = "hardware_otp"
CAPABILITY_MOBILE_PUSH = "mobile_push"


@dataclass(frozen=True)
class AuthFactor:
    """一个已登记的认证因素。

    :param factor_id: 因素唯一标识。
    :param capabilities: 该因素可提供的认证能力集合。
    :param device_id: 物理设备标识；相同值表示同一物理设备。
    :param recovery_channel_id: 恢复/重置渠道标识；``None`` 表示无恢复渠道，
        两个都为 ``None`` 的因素不因此产生关联。
    :param step_count: 使用该因素完成一次确认所需的用户操作步骤数，必须 >= 1。
    :param enabled: 因素是否处于启用状态；停用因素不参与规划。
    """

    factor_id: str
    capabilities: FrozenSet[str]
    device_id: str
    recovery_channel_id: str | None = None
    step_count: int = 1
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.factor_id, str) or not self.factor_id:
            raise ValueError("factor_id 必须是非空字符串")
        if not self.capabilities:
            raise ValueError(f"因素 {self.factor_id!r} 至少要声明一种认证能力")
        if not isinstance(self.device_id, str) or not self.device_id:
            raise ValueError(f"因素 {self.factor_id!r} 的 device_id 必须是非空字符串")
        if not isinstance(self.step_count, int) or self.step_count < 1:
            raise ValueError(
                f"因素 {self.factor_id!r} 的 step_count 必须是 >= 1 的整数"
            )
        # dataclass 是 frozen 的，用 object.__setattr__ 做规范化。
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))


@dataclass(frozen=True)
class OperationRequirement:
    """一次敏感操作的认证要求。

    :param operation_id: 操作唯一标识。
    :param required_capabilities: 必须被方案覆盖的认证能力集合。
    :param min_independent_proofs: 方案至少要提供的相互独立证明数（即冲突图
        连通分量数下限），必须 >= 1。
    """

    operation_id: str
    required_capabilities: FrozenSet[str]
    min_independent_proofs: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not self.operation_id:
            raise ValueError("operation_id 必须是非空字符串")
        if not self.required_capabilities:
            raise ValueError(
                f"操作 {self.operation_id!r} 至少要声明一种所需认证能力"
            )
        if not isinstance(self.min_independent_proofs, int) or self.min_independent_proofs < 1:
            raise ValueError("min_independent_proofs 必须是 >= 1 的整数")
        object.__setattr__(
            self, "required_capabilities", frozenset(self.required_capabilities)
        )


@dataclass(frozen=True)
class MissingCapability:
    """无可行方案时，对某一项缺失能力的诊断。"""

    capability: str
    available: bool
    """启用因素中是否存在任何因素能提供该能力（False = 完全未登记/已停用）。"""
    max_independent_sources: int
    """仅使用能提供该能力的启用因素时，最多能构成多少个相互独立来源。"""
    single_source: bool
    """该能力是否只能由一个独立来源提供（存在即可用，但永远无法与其他因素
    形成第二份独立证明）。"""


@dataclass(frozen=True)
class PlanResult:
    """规划结果。

    可行时 ``feasible`` 为 True，``selected_factors`` 为最优方案；
    不可行时 ``feasible`` 为 False，``missing`` 给出缺少的独立能力诊断，
    ``independence_deficit`` 为独立性缺口。
    """

    feasible: bool
    selected_factors: tuple[AuthFactor, ...] = ()
    total_steps: int = 0
    independent_proofs: int = 0
    covered_capabilities: FrozenSet[str] = frozenset()
    missing: tuple[MissingCapability, ...] = ()
    independence_deficit: int = 0
    reason: str = ""

    @property
    def factor_ids(self) -> tuple[str, ...]:
        return tuple(f.factor_id for f in self.selected_factors)
