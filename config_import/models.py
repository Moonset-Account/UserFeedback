"""校验模块的纯数据对象。

全部为 frozen dataclass，不携带任何行为；绑定结果以这些对象表示，
字段值使用深拷贝，保证校验过程不修改调用方的原始输入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RejectionCode(str, Enum):
    """结构化拒绝码。"""

    UNKNOWN_TYPE = "UNKNOWN_TYPE"
    UNKNOWN_SCHEMA_VERSION = "UNKNOWN_SCHEMA_VERSION"
    MALFORMED_PAYLOAD = "MALFORMED_PAYLOAD"
    UNKNOWN_AUTH_SOURCE = "UNKNOWN_AUTH_SOURCE"
    UNTRUSTED_AUTH_SOURCE = "UNTRUSTED_AUTH_SOURCE"
    TYPE_NOT_GRANTED = "TYPE_NOT_GRANTED"
    SERVER_FIELD_FORBIDDEN = "SERVER_FIELD_FORBIDDEN"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    ALIAS_CONFLICT = "ALIAS_CONFLICT"
    TYPE_MISMATCH = "TYPE_MISMATCH"


class FieldKind(str, Enum):
    SCALAR = "scalar"
    NESTED = "nested"
    ARRAY = "array"


@dataclass(frozen=True)
class FieldSpec:
    """单个字段的模式定义。

    writable=True  表示用户可更新的显示字段；
    writable=False 表示只能由服务端设置的内部字段。
    nested_target 仅 nested 字段使用，指向另一个对象类型；
    item_type 仅 array 字段使用，标量为 Python 类型，嵌套对象为类型名字符串。
    """

    name: str
    kind: FieldKind
    writable: bool
    scalar_type: type | None = None
    nested_target: str | None = None
    item_type: Any = None


@dataclass(frozen=True)
class SchemaVersion:
    """某个对象类型的一个版本化模式。"""

    type_name: str
    version: str
    fields: dict[str, FieldSpec]
    changelog: str = ""

    def field(self, name: str) -> FieldSpec | None:
        return self.fields.get(name)


@dataclass(frozen=True)
class ImportRecord:
    """一条待导入记录（输入侧）。

    schema_version 为 None 时由注册表解析该类型的默认（最新）版本。
    existing_state 为服务端当前已持久化的对象，用于保留服务端字段原值。
    """

    record_id: str
    type_name: str
    payload: dict[str, Any]
    auth_source: str
    schema_version: str | None = None
    existing_state: dict[str, Any] | None = None


@dataclass(frozen=True)
class Rejection:
    """一次结构化拒绝。

    始终携带生效的模式版本（若已解析）、字段路径（记录级拒绝为 $）
    以及触发的授权/校验规则 ID。
    """

    code: RejectionCode
    field_path: str
    rule_id: str
    message: str
    schema_version: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FieldDecision:
    """单个字段路径上的绑定判定，构成可回溯的审计轨迹。"""

    field_path: str
    action: str  # accepted / merged_container / merged_array
    schema_version: str
    rule_id: str
    writable: bool
    source_key: str
    alias_rule_id: str | None = None
    bound_value: Any = None


@dataclass(frozen=True)
class ValidationResult:
    """一条记录的完整校验 / 绑定结果（纯数据对象）。"""

    record_id: str
    type_name: str
    schema_version: str | None
    accepted: bool
    rejections: tuple[Rejection, ...] = ()
    decisions: tuple[FieldDecision, ...] = ()
    bound_object: dict[str, Any] | None = None
