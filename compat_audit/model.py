"""核查组件的领域模型与枚举定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class UnknownFieldPolicy(str, Enum):
    """未声明（未知）字段的反序列化处理策略。"""

    PRESERVE = "preserve"  # 保留未声明字段（本核查关注的危险行为）
    REJECT = "reject"      # 拒绝整条消息
    DROP = "drop"          # 丢弃未声明字段后继续解析


class SourceType(str, Enum):
    """某个生效选项值的来源类型。"""

    LAYER = "layer"                    # 来自具名配置层的显式设置
    VERSION_DEFAULT = "version_default"  # 来自协议版本默认值
    BUILTIN_DEFAULT = "builtin_default"  # 所有层与版本默认均未定义时的内建兜底


class PairStatus(str, Enum):
    """生产者/消费者组合的兼容结论。"""

    COMPATIBLE = "compatible"  # 重叠窗口内全部单元通过
    BLOCKED = "blocked"        # 存在必须阻断的策略冲突
    NO_OVERLAP = "no_overlap"  # 双方协议版本范围无交集，不构成升级窗口


class ReasonCode(str, Enum):
    """阻断/告警原因码。显式禁用与未定义使用不同原因码。"""

    UNDECLARED_FIELDS_RETAINED = "undeclared_fields_retained"
    # 消息类型要求 reject 未声明字段，生效策略却是 preserve
    POLICY_NOT_ALLOWED = "policy_not_allowed"
    # 生效策略不在该消息类型允许的策略集合内
    SAFEGUARD_EXPLICITLY_DISABLED = "safeguard_explicitly_disabled"
    # 消息类型要求开启的保护项被配置层显式关闭
    SAFEGUARD_UNDEFINED = "safeguard_undefined"
    # 保护项在任何配置层均未定义，经版本默认/内建兜底得到关闭值


# 可配置选项名
OPTION_UNKNOWN_FIELDS = "unknown_fields"
OPTION_EXTRA_VALIDATION = "extra_validation"
ALL_OPTIONS = (OPTION_UNKNOWN_FIELDS, OPTION_EXTRA_VALIDATION)

# 选项未在任何层、任何版本默认中定义时的内建兜底值。
# 内建默认对 unknown_fields 取最宽松的 preserve，因此“未定义”本身需要被显式追踪。
BUILTIN_DEFAULTS: dict[str, Any] = {
    OPTION_UNKNOWN_FIELDS: UnknownFieldPolicy.PRESERVE.value,
    OPTION_EXTRA_VALIDATION: False,
}


@dataclass(frozen=True)
class ProtocolVersion:
    name: str
    order: int


@dataclass(frozen=True)
class Layer:
    name: str
    priority: int


@dataclass(frozen=True)
class MessageTypeSpec:
    name: str
    # 该消息类型对未声明字段要求的策略；None 表示不强制
    required_unknown_field_policy: Optional[str]
    allowed_policies: list[str]
    require_extra_validation: bool = False


@dataclass(frozen=True)
class Setting:
    """单条配置层设置。

    version/message_type 为 None 表示通配范围。
    values 只保留显式定义的选项；JSON null 与缺失键在加载阶段即被剔除，
    以便把“未定义”与“显式禁用（布尔 false）”严格分开。
    """

    layer: str
    version: Optional[str]
    message_type: Optional[str]
    values: dict[str, Any]
    index: int


@dataclass(frozen=True)
class Endpoint:
    name: str
    role: str  # producer | consumer
    min_version: str
    max_version: str
    settings: list[Setting] = field(default_factory=list)


@dataclass(frozen=True)
class PairSpec:
    producer: str
    consumer: str
    message_types: list[str]


@dataclass(frozen=True)
class PolicySource:
    """生效值来源定位信息。"""

    source_type: str
    layer: Optional[str] = None
    layer_priority: Optional[int] = None
    version_scope: Optional[str] = None       # 设置条目声明的版本范围（None=通配）
    message_type_scope: Optional[str] = None  # 设置条目声明的消息类型范围
    setting_index: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.source_type,
            "layer": self.layer,
            "layer_priority": self.layer_priority,
            "version_scope": self.version_scope,
            "message_type_scope": self.message_type_scope,
            "setting_index": self.setting_index,
        }


@dataclass(frozen=True)
class ResolvedOption:
    """单个选项在某个端点/版本/消息类型上的解析结果。"""

    option: str
    value: Any
    defined: bool          # False = 任何配置层与版本默认均未定义，走内建兜底
    explicitly_disabled: bool  # 仅布尔选项可能为 True：配置层显式给出 false
    source: PolicySource

    def to_dict(self) -> dict[str, Any]:
        return {
            "option": self.option,
            "value": self.value,
            "defined": self.defined,
            "explicitly_disabled": self.explicitly_disabled,
            "source": self.source.to_dict(),
        }


@dataclass(frozen=True)
class Finding:
    reason_code: str
    endpoint: str
    role: str
    version: str
    message_type: str
    option: str
    effective_value: Any
    source: PolicySource
    required_value: Any = None
    allowed_values: Optional[list[str]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "endpoint": self.endpoint,
            "role": self.role,
            "version": self.version,
            "message_type": self.message_type,
            "option": self.option,
            "effective_value": self.effective_value,
            "required_value": self.required_value,
            "allowed_values": self.allowed_values,
            "source": self.source.to_dict(),
        }


@dataclass(frozen=True)
class CellResult:
    """重叠窗口内一个（版本 × 消息类型）单元的双边解析明细。"""

    version: str
    message_type: str
    producer: dict[str, ResolvedOption]
    consumer: dict[str, ResolvedOption]
    findings: list[Finding]

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "message_type": self.message_type,
            "ok": self.ok,
            "producer": {k: v.to_dict() for k, v in self.producer.items()},
            "consumer": {k: v.to_dict() for k, v in self.consumer.items()},
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass(frozen=True)
class PairConclusion:
    producer: str
    consumer: str
    message_types: list[str]
    status: str
    overlap_versions: list[str]
    cells: list[CellResult]
    findings: list[Finding]

    @property
    def pair_id(self) -> str:
        return f"{self.producer} -> {self.consumer}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair": self.pair_id,
            "producer": self.producer,
            "consumer": self.consumer,
            "message_types": list(self.message_types),
            "status": self.status,
            "overlap_versions": list(self.overlap_versions),
            "findings": [f.to_dict() for f in self.findings],
            "cells": [c.to_dict() for c in self.cells],
        }


@dataclass(frozen=True)
class AuditConfig:
    versions: list[ProtocolVersion]
    version_defaults: dict[str, dict[str, Any]]
    layers: list[Layer]
    message_types: list[MessageTypeSpec]
    endpoints: list[Endpoint]
    pairs: list[PairSpec]


@dataclass(frozen=True)
class AuditReport:
    conclusions: list[PairConclusion]

    @property
    def summary(self) -> dict[str, int]:
        counts = {s.value: 0 for s in PairStatus}
        for c in self.conclusions:
            counts[c.status] += 1
        return {"total": len(self.conclusions), **counts}

    @property
    def has_blocking(self) -> bool:
        return any(c.status == PairStatus.BLOCKED.value for c in self.conclusions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "pairs": [c.to_dict() for c in self.conclusions],
        }
