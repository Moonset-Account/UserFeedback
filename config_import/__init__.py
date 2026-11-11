"""配置导入服务 —— 绑定前校验模块。

公共 API：
    validate_record / validate_batch      校验入口
    ImportRecord / ValidationResult ...   纯数据对象
    SCHEMA_REGISTRY / AUTH_SOURCES ...    模式、别名、授权注册表
"""

from .models import (
    FieldDecision,
    FieldSpec,
    ImportRecord,
    Rejection,
    RejectionCode,
    SchemaVersion,
    ValidationResult,
)
from .engine import validate_batch, validate_record
from .schemas import SCHEMA_REGISTRY, DEFAULT_VERSIONS, get_schema, get_latest_schema
from .aliases import ALIAS_TABLES, AliasMapping, resolve_alias
from .authz import AUTH_SOURCES, AuthSource

__all__ = [
    "FieldDecision",
    "FieldSpec",
    "ImportRecord",
    "Rejection",
    "RejectionCode",
    "SchemaVersion",
    "ValidationResult",
    "validate_batch",
    "validate_record",
    "SCHEMA_REGISTRY",
    "DEFAULT_VERSIONS",
    "get_schema",
    "get_latest_schema",
    "ALIAS_TABLES",
    "AliasMapping",
    "resolve_alias",
    "AUTH_SOURCES",
    "AuthSource",
]
