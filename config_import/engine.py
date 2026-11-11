"""绑定前校验引擎。

校验流程（单条记录）：
  1. 按 type_name / schema_version 解析生效的版本化模式；
  2. 校验授权来源：来源须存在、受信任、且被授予该类型；
  3. 在每一层递归中解析字段别名，检测同层别名冲突；
  4. 逐字段判定：未知字段拒绝、server_only 字段拒绝、标量类型检查；
     nested / array 字段递归处理；
  5. 通过的字段深拷贝后叠加到现有状态上模拟绑定，服务端字段保留原值；
  6. 任何拒绝都会使整条记录被拒（bound_object 为 None），且原始输入绝不被修改。
"""

from __future__ import annotations

import copy
from typing import Any

from . import authz
from .aliases import resolve_alias
from .models import (
    FieldDecision,
    FieldKind,
    ImportRecord,
    Rejection,
    RejectionCode,
    SchemaVersion,
    ValidationResult,
)
from .schemas import SCHEMA_REGISTRY, get_latest_schema, get_schema

ROOT_PATH = "$"


def validate_batch(records: list[ImportRecord]) -> list[ValidationResult]:
    """批量校验：每条记录独立判定，一条不合规不影响其它记录。"""
    return [validate_record(record) for record in records]


def validate_record(record: ImportRecord) -> ValidationResult:
    rejections: list[Rejection] = []
    decisions: list[FieldDecision] = []

    # --- 1. 模式解析 -------------------------------------------------------
    if not isinstance(record.payload, dict):
        rejections.append(
            Rejection(
                code=RejectionCode.MALFORMED_PAYLOAD,
                field_path=ROOT_PATH,
                rule_id=authz.RULE_MALFORMED_PAYLOAD,
                message="payload 必须是 JSON 对象",
                schema_version=record.schema_version,
                detail={"actual_type": type(record.payload).__name__},
            )
        )
        return _failed(record, rejections, decisions)

    if record.type_name not in _top_level_types():
        rejections.append(
            Rejection(
                code=RejectionCode.UNKNOWN_TYPE,
                field_path=ROOT_PATH,
                rule_id=authz.RULE_UNKNOWN_TYPE,
                message=f"未知对象类型: {record.type_name!r}",
                schema_version=record.schema_version,
                detail={"type_name": record.type_name},
            )
        )
        return _failed(record, rejections, decisions)

    requested_version = record.schema_version
    if requested_version is None:
        schema = get_latest_schema(record.type_name)
    else:
        schema = get_schema(record.type_name, requested_version)

    if schema is None:
        rejections.append(
            Rejection(
                code=RejectionCode.UNKNOWN_SCHEMA_VERSION,
                field_path=ROOT_PATH,
                rule_id=authz.RULE_UNKNOWN_VERSION,
                message=(
                    f"类型 {record.type_name!r} 不存在模式版本 "
                    f"{requested_version!r}"
                ),
                schema_version=requested_version,
                detail={
                    "type_name": record.type_name,
                    "available_versions": sorted(
                        SCHEMA_REGISTRY[record.type_name]
                    ),
                },
            )
        )
        return _failed(record, rejections, decisions)

    # --- 2. 授权来源校验 ---------------------------------------------------
    source = authz.get_auth_source(record.auth_source)
    if source is None:
        rejections.append(
            Rejection(
                code=RejectionCode.UNKNOWN_AUTH_SOURCE,
                field_path=ROOT_PATH,
                rule_id=authz.RULE_SOURCE_UNTRUSTED,
                message=f"授权来源不明: {record.auth_source!r}",
                schema_version=schema.version,
                detail={"auth_source": record.auth_source},
            )
        )
        return _failed(record, rejections, decisions, schema.version)

    if not source.trusted:
        rejections.append(
            Rejection(
                code=RejectionCode.UNTRUSTED_AUTH_SOURCE,
                field_path=ROOT_PATH,
                rule_id=authz.RULE_SOURCE_UNTRUSTED,
                message=f"授权来源 {source.source_id!r} 已失去信任",
                schema_version=schema.version,
                detail={"auth_source": source.source_id},
            )
        )
        return _failed(record, rejections, decisions, schema.version)

    if record.type_name not in source.type_grants:
        rejections.append(
            Rejection(
                code=RejectionCode.TYPE_NOT_GRANTED,
                field_path=ROOT_PATH,
                rule_id=authz.RULE_TYPE_NOT_GRANTED,
                message=(
                    f"来源 {source.source_id!r} 未被授予类型 "
                    f"{record.type_name!r}（授权规则 {source.grant_rule_id}）"
                ),
                schema_version=schema.version,
                detail={
                    "auth_source": source.source_id,
                    "grant_rule_id": source.grant_rule_id,
                    "granted_types": sorted(source.type_grants),
                },
            )
        )
        return _failed(record, rejections, decisions)

    # --- 3/4/5. 递归字段校验 + 模拟绑定 ------------------------------------
    bound = copy.deepcopy(record.existing_state) if record.existing_state else {}
    _walk_object(
        type_name=schema.type_name,
        schema=schema,
        payload=record.payload,
        bound=bound,
        path=ROOT_PATH,
        rejections=rejections,
        decisions=decisions,
    )

    if rejections:
        return _failed(record, rejections, decisions, schema.version)

    return ValidationResult(
        record_id=record.record_id,
        type_name=record.type_name,
        schema_version=schema.version,
        accepted=True,
        rejections=(),
        decisions=tuple(decisions),
        bound_object=bound,
    )


# ---------------------------------------------------------------------------
# 递归核心
# ---------------------------------------------------------------------------
def _walk_object(
    *,
    type_name: str,
    schema: SchemaVersion,
    payload: dict[str, Any],
    bound: dict[str, Any],
    path: str,
    rejections: list[Rejection],
    decisions: list[FieldDecision],
) -> None:
    """对一层对象做别名解析、冲突检测与逐字段递归校验。"""
    if not isinstance(payload, dict):
        rejections.append(
            Rejection(
                code=RejectionCode.MALFORMED_PAYLOAD,
                field_path=path,
                rule_id=authz.RULE_MALFORMED_PAYLOAD,
                message=f"{path} 处应为 JSON 对象，实际为 {type(payload).__name__}",
                schema_version=schema.version,
                detail={"actual_type": type(payload).__name__},
            )
        )
        return

    # --- 别名解析与同层冲突检测 -------------------------------------------
    # canonical_key -> (source_key, value, alias_rule_id or None)
    resolved: dict[str, tuple[str, Any, str | None]] = {}
    conflicted: set[str] = set()
    for source_key, value in payload.items():
        mapping = resolve_alias(type_name, schema.version, source_key)
        if mapping is not None:
            canonical, alias_rule = mapping.canonical, mapping.rule_id
        elif source_key in schema.fields:
            canonical, alias_rule = source_key, None
        else:
            # 未知字段：不在别名表也不在模式中
            rejections.append(
                Rejection(
                    code=RejectionCode.UNKNOWN_FIELD,
                    field_path=_join(path, source_key),
                    rule_id=authz.RULE_UNKNOWN_FIELD,
                    message=(
                        f"类型 {type_name}@{schema.version} 不存在字段 "
                        f"{source_key!r}，也无兼容别名"
                    ),
                    schema_version=schema.version,
                    detail={"source_key": source_key},
                )
            )
            continue

        if canonical in resolved:
            prev_key, prev_value, _ = resolved[canonical]
            if prev_value != value:
                conflicted.add(canonical)
                rejections.append(
                    Rejection(
                        code=RejectionCode.ALIAS_CONFLICT,
                        field_path=_join(path, canonical),
                        rule_id=authz.RULE_ALIAS_CONFLICT,
                        message=(
                            f"互相冲突的别名/字段 {prev_key!r} 与 {source_key!r} "
                            f"同时映射到 {canonical!r} 且取值不同"
                        ),
                        schema_version=schema.version,
                        detail={
                            "canonical": canonical,
                            "source_keys": [prev_key, source_key],
                            "values": [prev_value, value],
                        },
                    )
                )
            # 值相同的重复来源不构成冲突，先写入的规范键优先
        else:
            resolved[canonical] = (source_key, value, alias_rule)

    # --- 逐字段权限与类型校验 ---------------------------------------------
    for canonical, (source_key, value, alias_rule) in resolved.items():
        field_path = _join(path, canonical)
        if canonical in conflicted:
            # 冲突已记录拒绝，跳过该字段绑定
            continue

        spec = schema.field(canonical)
        if spec is None:
            # 别名指向的规范字段在当前版本已移除（版本不兼容应在解析期被拦，
            # 这里是防御性分支）
            rejections.append(
                Rejection(
                    code=RejectionCode.UNKNOWN_FIELD,
                    field_path=field_path,
                    rule_id=authz.RULE_UNKNOWN_FIELD,
                    message=f"别名 {source_key!r} 指向未知规范字段 {canonical!r}",
                    schema_version=schema.version,
                    detail={"source_key": source_key, "canonical": canonical},
                )
            )
            continue

        if not spec.writable:
            rejections.append(
                Rejection(
                    code=RejectionCode.SERVER_FIELD_FORBIDDEN,
                    field_path=field_path,
                    rule_id=authz.RULE_SERVER_FIELD,
                    message=(
                        f"字段 {canonical!r} 为服务端内部字段，"
                        f"导入来源不可写（{authz.RULE_SERVER_FIELD}）"
                    ),
                    schema_version=schema.version,
                    detail={"source_key": source_key, "canonical": canonical},
                )
            )
            continue

        if spec.kind is FieldKind.SCALAR:
            if not _scalar_matches(value, spec.scalar_type):
                rejections.append(
                    Rejection(
                        code=RejectionCode.TYPE_MISMATCH,
                        field_path=field_path,
                        rule_id=authz.RULE_TYPE_MISMATCH,
                        message=(
                            f"字段 {canonical!r} 期望 {spec.scalar_type.__name__}，"
                            f"实际为 {type(value).__name__}"
                        ),
                        schema_version=schema.version,
                        detail={
                            "expected": spec.scalar_type.__name__,
                            "actual": type(value).__name__,
                        },
                    )
                )
                continue
            bound[canonical] = copy.deepcopy(value)
            decisions.append(
                FieldDecision(
                    field_path=field_path,
                    action="accepted",
                    schema_version=schema.version,
                    rule_id=authz.RULE_WRITABLE_FIELD,
                    writable=True,
                    source_key=source_key,
                    alias_rule_id=alias_rule,
                    bound_value=copy.deepcopy(value),
                )
            )

        elif spec.kind is FieldKind.NESTED:
            nested_schema = get_latest_schema(spec.nested_target)
            if nested_schema is None:
                rejections.append(
                    Rejection(
                        code=RejectionCode.UNKNOWN_TYPE,
                        field_path=field_path,
                        rule_id=authz.RULE_UNKNOWN_TYPE,
                        message=f"嵌套类型 {spec.nested_target!r} 未注册",
                        schema_version=schema.version,
                        detail={"nested_target": spec.nested_target},
                    )
                )
                continue
            nested_bound = bound.get(canonical)
            if not isinstance(nested_bound, dict):
                nested_bound = {}
            _walk_object(
                type_name=nested_schema.type_name,
                schema=nested_schema,
                payload=value,
                bound=nested_bound,
                path=field_path,
                rejections=rejections,
                decisions=decisions,
            )
            bound[canonical] = nested_bound
            decisions.append(
                FieldDecision(
                    field_path=field_path,
                    action="merged_container",
                    schema_version=nested_schema.version,
                    rule_id=authz.RULE_WRITABLE_FIELD,
                    writable=True,
                    source_key=source_key,
                    alias_rule_id=alias_rule,
                    bound_value=copy.deepcopy(nested_bound),
                )
            )

        else:  # FieldKind.ARRAY
            normalized = _check_array(
                spec=spec,
                value=value,
                field_path=field_path,
                parent_version=schema.version,
                rejections=rejections,
                decisions=decisions,
            )
            if normalized is None:
                continue
            bound[canonical] = normalized
            decisions.append(
                FieldDecision(
                    field_path=field_path,
                    action="merged_array",
                    schema_version=schema.version,
                    rule_id=authz.RULE_WRITABLE_FIELD,
                    writable=True,
                    source_key=source_key,
                    alias_rule_id=alias_rule,
                    bound_value=copy.deepcopy(normalized),
                )
            )


def _check_array(
    *,
    spec,
    value: Any,
    field_path: str,
    parent_version: str,
    rejections: list[Rejection],
    decisions: list[FieldDecision],
) -> list[Any] | None:
    """校验数组字段。

    成功时返回规范化后的新数组（对象元素的别名已解析为规范键）；
    任何元素出错时返回 None，错误已登记到 rejections。
    """
    if not isinstance(value, list):
        rejections.append(
            Rejection(
                code=RejectionCode.TYPE_MISMATCH,
                field_path=field_path,
                rule_id=authz.RULE_TYPE_MISMATCH,
                message=f"字段 {spec.name!r} 期望数组，实际为 {type(value).__name__}",
                schema_version=parent_version,
                detail={"expected": "array", "actual": type(value).__name__},
            )
        )
        return None

    item_type = spec.item_type
    if isinstance(item_type, str):
        # 对象数组：逐元素递归校验并收集规范化后的绑定结果
        element_schema = get_latest_schema(item_type)
        if element_schema is None:
            rejections.append(
                Rejection(
                    code=RejectionCode.UNKNOWN_TYPE,
                    field_path=field_path,
                    rule_id=authz.RULE_UNKNOWN_TYPE,
                    message=f"数组元素类型 {item_type!r} 未注册",
                    schema_version=parent_version,
                    detail={"item_type": item_type},
                )
            )
            return None
        normalized_elements: list[Any] = []
        for index, element in enumerate(value):
            element_path = f"{field_path}[{index}]"
            if not isinstance(element, dict):
                rejections.append(
                    Rejection(
                        code=RejectionCode.TYPE_MISMATCH,
                        field_path=element_path,
                        rule_id=authz.RULE_TYPE_MISMATCH,
                        message=(
                            f"数组元素应为对象，实际为 {type(element).__name__}"
                        ),
                        schema_version=element_schema.version,
                        detail={"actual": type(element).__name__},
                    )
                )
                return None
            # 数组元素按整体替换语义，没有既有子状态需要合并；
            # 元素内部的字段判定同样登记到顶层审计轨迹
            element_bound: dict[str, Any] = {}
            _walk_object(
                type_name=element_schema.type_name,
                schema=element_schema,
                payload=element,
                bound=element_bound,
                path=element_path,
                rejections=rejections,
                decisions=decisions,
            )
            normalized_elements.append(element_bound)
        return normalized_elements

    # 标量数组
    for index, element in enumerate(value):
        if not _scalar_matches(element, item_type):
            rejections.append(
                Rejection(
                    code=RejectionCode.TYPE_MISMATCH,
                    field_path=f"{field_path}[{index}]",
                    rule_id=authz.RULE_TYPE_MISMATCH,
                    message=(
                        f"数组元素期望 {item_type.__name__}，"
                        f"实际为 {type(element).__name__}"
                    ),
                    schema_version=parent_version,
                    detail={
                        "expected": item_type.__name__,
                        "actual": type(element).__name__,
                    },
                )
            )
            return None
    # 返回深拷贝，确保绑定对象与调用方输入互不影响
    return copy.deepcopy(value)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _scalar_matches(value: Any, expected: type | None) -> bool:
    if expected is None:
        return True
    # bool 是 int 的子类，JSON 布尔不能冒充整数
    if expected is int and isinstance(value, bool):
        return False
    return isinstance(value, expected)


def _join(path: str, key: str) -> str:
    # 根路径 ROOT_PATH 为 "$"；向下拼接时统一以 "$." 起始，
    # 嵌套层则在已有路径后追加 ".key"
    return f"$.{key}" if path == ROOT_PATH else f"{path}.{key}"


def _top_level_types() -> set[str]:
    # 嵌套类型（Preferences/Contact）不作为顶层导入类型
    return set(SCHEMA_REGISTRY) - {"Preferences", "Contact"}


def _failed(
    record: ImportRecord,
    rejections: list[Rejection],
    decisions: list[FieldDecision],
    effective_version: str | None = None,
) -> ValidationResult:
    return ValidationResult(
        record_id=record.record_id,
        type_name=record.type_name,
        schema_version=(
            effective_version if effective_version is not None else record.schema_version
        ),
        accepted=False,
        rejections=tuple(rejections),
        decisions=tuple(decisions),
        bound_object=None,
    )
