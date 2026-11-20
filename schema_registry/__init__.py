"""消息总线模式注册模块。

支持具名记录、可嵌套记录/数组、整数、字符串与可空字段；
新字段可声明旧名与缺省值；登记读取模式前逐一比较保留期内的
写入模式，全部兼容后才允许按同一规则读取旧消息。
"""
from .types import (
    INT,
    MISSING,
    STRING,
    ArrayType,
    Field,
    IntType,
    RecordType,
    SchemaType,
    StringType,
)
from .resolution import ResolutionError, resolve_record_fields
from .compat import Issue, check_record_compatible, validate_value
from .reader import ReadError, read_message
from .registry import CompatibilityError, RegistryError, SchemaRegistry

__all__ = [
    "INT",
    "MISSING",
    "STRING",
    "ArrayType",
    "Field",
    "IntType",
    "RecordType",
    "SchemaType",
    "StringType",
    "ResolutionError",
    "resolve_record_fields",
    "Issue",
    "check_record_compatible",
    "validate_value",
    "ReadError",
    "read_message",
    "CompatibilityError",
    "RegistryError",
    "SchemaRegistry",
]
