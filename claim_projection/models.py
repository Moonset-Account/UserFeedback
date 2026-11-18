"""声明投影模块的数据模型。

所有输入模型均为不可变值对象，支持以关键字参数或普通字典构造，
便于直接对接联合登录管线中反序列化后的 JSON 数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AbstractSet, FrozenSet, Mapping, Optional, Tuple


# ---------------------------------------------------------------------------
# 用途常量：联合登录应用允许声明使用的三类用途
# ---------------------------------------------------------------------------
PURPOSE_PROFILE = "profile"        # 姓名等基础身份资料
PURPOSE_EMPLOYMENT = "employment"  # 在岗/雇佣关系证明
PURPOSE_TEAM = "team"              # 团队与组织信息

ALL_PURPOSES: FrozenSet[str] = frozenset(
    {PURPOSE_PROFILE, PURPOSE_EMPLOYMENT, PURPOSE_TEAM}
)


@dataclass(frozen=True)
class Attribute:
    """一条已验证的身份属性。

    value 为 None 表示该属性缺失（未经提供）。verified 默认为 True，
    只有已验证属性才允许进入投影；未验证属性按不可用处理。
    """

    name: str
    value: object
    verified: bool = True

    @property
    def usable(self) -> bool:
        """属性是否可用于签发（已验证且取值存在）。"""
        return self.verified and self.value is not None

    @classmethod
    def from_value(cls, name: str, value: object) -> "Attribute":
        return cls(name=name, value=value, verified=True)


@dataclass(frozen=True)
class ClaimRequest:
    """受众对某条声明的一次请求。

    - claim_name: 声明目录中的声明名
    - required: True 为必需声明，无法授权时停止整次签发；
      False 为可选声明，无法授权时省略
    - require_detail: 请求方显式需要明细值而非布尔证明。
      缺省 False 时，若存在可授权的已验证布尔证明，则只发证明（最小化）。
    """

    claim_name: str
    required: bool = False
    require_detail: bool = False


@dataclass(frozen=True)
class ConsentRecord:
    """某一同意版本的完整快照。

    每次用户更新同意都会产生一条新的完整快照（而非增量补丁），
    granted_purposes 为该版本下用户授予的全部用途。投影只使用
    版本号最高的一条记录，保证整份投影基于同一个同意版本。
    revoked 为 True 表示该版本整体撤回（用户撤销所有授权）。
    """

    version: int
    granted_purposes: FrozenSet[str]
    revoked: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.version, int) or isinstance(self.version, bool):
            raise ValueError("consent version 必须为整数")
        if self.version < 1:
            raise ValueError("consent version 必须 >= 1")
        if not isinstance(self.granted_purposes, (frozenset, set, tuple, list)):
            raise ValueError("granted_purposes 必须为用途集合")
        # frozen dataclass 中通过 object.__setattr__ 归一化
        object.__setattr__(
            self,
            "granted_purposes",
            frozenset(self.granted_purposes),
        )
        if self.revoked and self.granted_purposes:
            raise ValueError("已撤回的同意版本不得再携带授权用途")

    def allows(self, purpose: str) -> bool:
        return not self.revoked and purpose in self.granted_purposes


@dataclass(frozen=True)
class BlockedClaim:
    """一条被阻止的声明及其原因。"""

    claim_name: str
    required: bool
    reason: str
    detail: str


@dataclass(frozen=True)
class ProjectionResult:
    """一次声明投影的结果。

    - claims: 允许输出的声明（按请求处理顺序排列）
    - omitted: 被省略的可选声明
    - blocked: 触发中止的必需声明（存在即表示本次签发被停止）
    - issued: 本次是否实际完成签发（无必需声明被阻止）
    - consent_version: 投影实际采用的同意版本（无同意记录时为 None）
    - effective_purposes: 受众允许且用户在当前版本同意的用途交集
    """

    claims: Mapping[str, object] = field(default_factory=dict)
    omitted: Tuple[BlockedClaim, ...] = ()
    blocked: Optional[BlockedClaim] = None
    issued: bool = True
    consent_version: Optional[int] = None
    effective_purposes: FrozenSet[str] = frozenset()

    @property
    def stopped(self) -> bool:
        """是否因必需字段无法授权而停止签发。"""
        return self.blocked is not None
