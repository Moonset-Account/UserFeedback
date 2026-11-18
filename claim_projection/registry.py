"""声明目录与属性-用途注册表。

设计要点（对应题面约束）：

1. 每个原始属性登记其访问所需用途；衍生字段需要哪些原始属性，
   就必须满足这些属性对应的全部用途授权。
2. 每条声明可携带一个可选的已验证布尔证明规格：当证明本身已
   验证且其用途已授权时，仅输出布尔证明，不展开明细（最小披露）。
3. 明细规格声明展开所需的原始属性；明细比证明多访问的任何属性，
   都要求额外的用途授权（例如 team 明细在 team_member 布尔证明
   之外还需要 team_id/team_name 的 team 用途）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, FrozenSet, Mapping, Optional, Tuple

from .models import (
    PURPOSE_EMPLOYMENT,
    PURPOSE_PROFILE,
    PURPOSE_TEAM,
)


# ---------------------------------------------------------------------------
# 原始属性 -> 访问该属性所需用途
# ---------------------------------------------------------------------------
DEFAULT_ATTRIBUTE_PURPOSES: Mapping[str, FrozenSet[str]] = {
    "given_name": frozenset({PURPOSE_PROFILE}),
    "family_name": frozenset({PURPOSE_PROFILE}),
    "is_active_employee": frozenset({PURPOSE_EMPLOYMENT}),
    "employee_id": frozenset({PURPOSE_EMPLOYMENT}),
    "is_team_member": frozenset({PURPOSE_TEAM}),
    "team_id": frozenset({PURPOSE_TEAM}),
    "team_name": frozenset({PURPOSE_TEAM}),
}


@dataclass(frozen=True)
class BooleanProofSpec:
    """已验证布尔证明规格。

    source_attribute 必须是布尔型已验证属性；输出声明直接取其布尔值。
    """

    source_attribute: str
    purposes: FrozenSet[str]


@dataclass(frozen=True)
class ClaimSpec:
    """声明目录中的一条声明规格。

    - name: 对外声明名
    - proof: 可选的布尔证明规格（可仅以证明满足请求）
    - detail_attributes: 展开明细所需的原始属性
    - detail_purposes: 明细额外/全部依赖的用途（默认由属性注册表推导）
    - detail_builder: 由属性值构造明细声明的函数
    - partial_detail: True 时允许部分原始属性缺失，由 builder 尽力构造
      （适用于姓名等可由任一组成部分生成的声明）；默认 False，
      任一必需原始属性缺失即按 attribute_missing 处理
    - attribute_purposes: 属性->用途注册表（默认使用全局注册表）
    """

    name: str
    detail_attributes: Tuple[str, ...] = ()
    detail_builder: Optional[Callable[[Mapping[str, object]], object]] = None
    proof: Optional[BooleanProofSpec] = None
    detail_purposes: Optional[FrozenSet[str]] = None
    partial_detail: bool = False
    attribute_purposes: Mapping[str, FrozenSet[str]] = field(
        default_factory=lambda: DEFAULT_ATTRIBUTE_PURPOSES
    )

    def required_detail_purposes(self) -> FrozenSet[str]:
        """展开明细所需的全部用途。"""
        if self.detail_purposes is not None:
            return frozenset(self.detail_purposes)
        purposes: set[str] = set()
        for attribute in self.detail_attributes:
            purposes.update(self.attribute_purposes.get(attribute, frozenset()))
        return frozenset(purposes)


def _build_full_name(values: Mapping[str, object]) -> object:
    given = values.get("given_name")
    family = values.get("family_name")
    parts = [part for part in (given, family) if part]
    if not parts:
        return None
    return " ".join(str(part) for part in parts)


def _build_employment_detail(values: Mapping[str, object]) -> object:
    # 明细路径与布尔证明解耦：证明承担在岗布尔状态，明细只承载工号
    employee_id = values.get("employee_id")
    if employee_id is None:
        return None
    return {"employee_id": employee_id}


def _build_team_detail(values: Mapping[str, object]) -> object:
    # 明细路径与布尔证明解耦：证明承担是否在团队，明细承载团队标识与名称
    detail = {}
    if values.get("team_id") is not None:
        detail["team_id"] = values["team_id"]
    if values.get("team_name") is not None:
        detail["team_name"] = values["team_name"]
    return detail or None


def _build_employment_verification_detail(values: Mapping[str, object]) -> object:
    """跨用途衍生字段：同时依赖雇佣与团队原始属性。

    用于验证"衍生字段只要需要访问额外原始属性，就要满足相应用途授权"：
    除 employment 用途外，还需 team 用途才能访问团队属性。
    """
    detail = {
        "active_employee": bool(values.get("is_active_employee")),
        "employee_id": values.get("employee_id"),
        "team_member": bool(values.get("is_team_member")),
        "team_id": values.get("team_id"),
    }
    return detail


# ---------------------------------------------------------------------------
# 默认声明目录
# ---------------------------------------------------------------------------
DEFAULT_CLAIM_REGISTRY: Mapping[str, ClaimSpec] = {
    # 姓名：基础资料用途，无布尔证明可替代，始终是明细声明
    "name": ClaimSpec(
        name="name",
        detail_attributes=("given_name", "family_name"),
        detail_builder=_build_full_name,
        partial_detail=True,
    ),

    # 在岗证明：存在已验证布尔证明 is_active_employee，
    # 默认只发布尔证明；显式要求明细时才展开工号等雇佣信息
    "employment_status": ClaimSpec(
        name="employment_status",
        detail_attributes=("employee_id",),
        detail_builder=_build_employment_detail,
        proof=BooleanProofSpec(
            source_attribute="is_active_employee",
            purposes=frozenset({PURPOSE_EMPLOYMENT}),
        ),
    ),

    # 团队信息：布尔证明 is_team_member 可单独满足"是否在团队"类请求；
    # 明细需要额外访问 team_id/team_name，要求 team 用途授权
    "team": ClaimSpec(
        name="team",
        detail_attributes=("team_id", "team_name"),
        detail_builder=_build_team_detail,
        proof=BooleanProofSpec(
            source_attribute="is_team_member",
            purposes=frozenset({PURPOSE_TEAM}),
        ),
    ),

    # 跨用途衍生字段：雇佣 + 团队联合验证
    "employment_verification": ClaimSpec(
        name="employment_verification",
        detail_attributes=(
            "is_active_employee",
            "employee_id",
            "is_team_member",
            "team_id",
        ),
        detail_builder=_build_employment_verification_detail,
        detail_purposes=frozenset({PURPOSE_EMPLOYMENT, PURPOSE_TEAM}),
    ),
}
