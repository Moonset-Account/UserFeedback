"""配置加载与校验。

把原始字典转换为 :class:`~compat_audit.model.AuditConfig`。

三态语义（题面：“选项未定义与显式禁用要分开处理”）：

* 键缺失或值为 JSON null —— 该层条目未定义此选项，不写入 Setting.values；
* 布尔选项值为 ``false`` —— 显式禁用，必须保留并在核查中单独归因；
* 枚举选项值为具体策略名 —— 显式配置，优先于版本默认值。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model import (
    ALL_OPTIONS,
    AuditConfig,
    Endpoint,
    Layer,
    MessageTypeSpec,
    OPTION_EXTRA_VALIDATION,
    OPTION_UNKNOWN_FIELDS,
    PairSpec,
    ProtocolVersion,
    Setting,
    UnknownFieldPolicy,
)

_ROLES = frozenset({"producer", "consumer"})


class ConfigError(ValueError):
    """配置结构或取值非法。"""


def load_config(path: str | Path) -> AuditConfig:
    """从 JSON 文件加载配置（不执行其中任何代码，仅做数据解析）。"""
    text = Path(path).read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON: {exc}") from exc
    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> AuditConfig:
    if not isinstance(raw, dict):
        raise ConfigError("配置根节点必须是对象")

    versions = _parse_versions(raw.get("versions"))
    version_index = {v.name: v for v in versions}
    layers = _parse_layers(raw.get("layers"))
    layer_index = {l.name: l for l in layers}
    version_defaults = _parse_version_defaults(
        raw.get("version_defaults", {}), version_index
    )
    message_types = _parse_message_types(raw.get("message_types", []))
    mt_index = {m.name: m for m in message_types}
    endpoints = _parse_endpoints(
        raw.get("endpoints", []), version_index, layer_index, mt_index
    )
    ep_index = {e.name: e for e in endpoints}
    pairs = _parse_pairs(raw.get("pairs", []), ep_index, mt_index)

    return AuditConfig(
        versions=versions,
        version_defaults=version_defaults,
        layers=layers,
        message_types=message_types,
        endpoints=endpoints,
        pairs=pairs,
    )


def _parse_versions(raw: Any) -> list[ProtocolVersion]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("versions 必须是非空数组")
    seen: set[str] = set()
    result: list[ProtocolVersion] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"versions[{i}] 必须是对象")
        name = item.get("name")
        order = item.get("order")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"versions[{i}].name 必须是非空字符串")
        if name in seen:
            raise ConfigError(f"版本名重复: {name}")
        if not isinstance(order, int) or isinstance(order, bool):
            raise ConfigError(f"versions[{i}].order 必须是整数")
        seen.add(name)
        result.append(ProtocolVersion(name=name, order=order))
    return result


def _parse_layers(raw: Any) -> list[Layer]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("layers 必须是非空数组")
    seen: set[str] = set()
    result: list[Layer] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"layers[{i}] 必须是对象")
        name = item.get("name")
        priority = item.get("priority")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"layers[{i}].name 必须是非空字符串")
        if name in seen:
            raise ConfigError(f"配置层名重复: {name}")
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise ConfigError(f"layers[{i}].priority 必须是整数")
        seen.add(name)
        result.append(Layer(name=name, priority=priority))
    return result


def _parse_version_defaults(
    raw: Any, version_index: dict[str, ProtocolVersion]
) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise ConfigError("version_defaults 必须是对象")
    result: dict[str, dict[str, Any]] = {}
    for version, opts in raw.items():
        if version not in version_index:
            raise ConfigError(f"version_defaults 引用了未定义版本: {version}")
        if not isinstance(opts, dict):
            raise ConfigError(f"version_defaults.{version} 必须是对象")
        result[version] = _sanitize_values(opts, f"version_defaults.{version}")
    return result


def _parse_message_types(raw: Any) -> list[MessageTypeSpec]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("message_types 必须是非空数组")
    seen: set[str] = set()
    result: list[MessageTypeSpec] = []
    valid = {p.value for p in UnknownFieldPolicy}
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"message_types[{i}] 必须是对象")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"message_types[{i}].name 必须是非空字符串")
        if name in seen:
            raise ConfigError(f"消息类型名重复: {name}")
        seen.add(name)

        required = item.get("required_unknown_field_policy")
        if required is not None and required not in valid:
            raise ConfigError(
                f"message_types[{name}].required_unknown_field_policy 非法: {required}"
            )
        allowed = item.get("allowed_policies")
        if not isinstance(allowed, list) or not allowed:
            raise ConfigError(
                f"message_types[{name}].allowed_policies 必须是非空数组"
            )
        for p in allowed:
            if p not in valid:
                raise ConfigError(
                    f"message_types[{name}].allowed_policies 含非法策略: {p}"
                )
        if required is not None and required not in allowed:
            raise ConfigError(
                f"message_types[{name}] 的强制策略 {required} 不在允许集合内"
            )
        require_ev = bool(item.get("require_extra_validation", False))
        result.append(
            MessageTypeSpec(
                name=name,
                required_unknown_field_policy=required,
                allowed_policies=list(allowed),
                require_extra_validation=require_ev,
            )
        )
    return result


def _parse_endpoints(
    raw: Any,
    version_index: dict[str, ProtocolVersion],
    layer_index: dict[str, Layer],
    mt_index: dict[str, MessageTypeSpec],
) -> list[Endpoint]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("endpoints 必须是非空数组")
    seen: set[str] = set()
    result: list[Endpoint] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"endpoints[{i}] 必须是对象")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"endpoints[{i}].name 必须是非空字符串")
        if name in seen:
            raise ConfigError(f"端点名重复: {name}")
        seen.add(name)

        role = item.get("role")
        if role not in _ROLES:
            raise ConfigError(f"endpoints[{name}].role 必须是 producer 或 consumer")

        min_v = item.get("min_version")
        max_v = item.get("max_version")
        if min_v not in version_index or max_v not in version_index:
            raise ConfigError(
                f"endpoints[{name}] 的版本范围引用了未定义版本: {min_v}..{max_v}"
            )
        if version_index[min_v].order > version_index[max_v].order:
            raise ConfigError(
                f"endpoints[{name}] 的 min_version 高于 max_version"
            )

        settings = _parse_settings(
            item.get("settings", []), name, layer_index, version_index, mt_index
        )
        result.append(
            Endpoint(
                name=name,
                role=role,
                min_version=min_v,
                max_version=max_v,
                settings=settings,
            )
        )
    return result


def _parse_settings(
    raw: Any,
    endpoint_name: str,
    layer_index: dict[str, Layer],
    version_index: dict[str, ProtocolVersion],
    mt_index: dict[str, MessageTypeSpec],
) -> list[Setting]:
    if not isinstance(raw, list):
        raise ConfigError(f"endpoints[{endpoint_name}].settings 必须是数组")
    result: list[Setting] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(
                f"endpoints[{endpoint_name}].settings[{i}] 必须是对象"
            )
        layer = item.get("layer")
        if layer not in layer_index:
            raise ConfigError(
                f"endpoints[{endpoint_name}].settings[{i}] 引用了未定义配置层: {layer}"
            )
        v_scope = item.get("version")
        if v_scope is not None and v_scope not in version_index:
            raise ConfigError(
                f"endpoints[{endpoint_name}].settings[{i}] 引用了未定义版本: {v_scope}"
            )
        mt_scope = item.get("message_type")
        if mt_scope is not None and mt_scope not in mt_index:
            raise ConfigError(
                f"endpoints[{endpoint_name}].settings[{i}] 引用了未定义消息类型: {mt_scope}"
            )
        values = item.get("values", {})
        if not isinstance(values, dict):
            raise ConfigError(
                f"endpoints[{endpoint_name}].settings[{i}].values 必须是对象"
            )
        clean = _sanitize_values(
            values, f"endpoints[{endpoint_name}].settings[{i}].values"
        )
        if not clean:
            # 全是 null/未知键的条目没有任何显式定义，保留它只会制造噪音
            continue
        result.append(
            Setting(
                layer=layer,
                version=v_scope,
                message_type=mt_scope,
                values=clean,
                index=i,
            )
        )
    return result


def _sanitize_values(raw: dict[str, Any], path: str) -> dict[str, Any]:
    """剔除未定义项（缺键/null），校验显式值的类型与取值。

    显式 ``false`` 必须保留，它与“未定义”是两种不同的语义。
    """
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in ALL_OPTIONS:
            raise ConfigError(f"{path} 含未知选项: {key}")
        if value is None:
            continue  # 未定义：不写入，后续走版本默认/内建兜底
        if key == OPTION_UNKNOWN_FIELDS:
            if not isinstance(value, str) or value not in {
                p.value for p in UnknownFieldPolicy
            }:
                raise ConfigError(
                    f"{path}.{key} 必须是 preserve/reject/drop 或 null"
                )
        elif key == OPTION_EXTRA_VALIDATION:
            if not isinstance(value, bool):
                raise ConfigError(f"{path}.{key} 必须是布尔值或 null")
        clean[key] = value
    return clean


def _parse_pairs(
    raw: Any,
    ep_index: dict[str, Endpoint],
    mt_index: dict[str, MessageTypeSpec],
) -> list[PairSpec]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("pairs 必须是非空数组")
    result: list[PairSpec] = []
    seen: set[tuple[str, str]] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"pairs[{i}] 必须是对象")
        producer = item.get("producer")
        consumer = item.get("consumer")
        if producer not in ep_index or consumer not in ep_index:
            raise ConfigError(
                f"pairs[{i}] 引用了未定义端点: {producer} / {consumer}"
            )
        if ep_index[producer].role != "producer":
            raise ConfigError(f"pairs[{i}].producer 端点名 {producer} 角色不是 producer")
        if ep_index[consumer].role != "consumer":
            raise ConfigError(f"pairs[{i}].consumer 端点名 {consumer} 角色不是 consumer")
        key = (producer, consumer)
        if key in seen:
            raise ConfigError(f"pairs 中存在重复组合: {producer} -> {consumer}")
        seen.add(key)
        mts = item.get("message_types")
        if not isinstance(mts, list) or not mts:
            raise ConfigError(f"pairs[{i}].message_types 必须是非空数组")
        for mt in mts:
            if mt not in mt_index:
                raise ConfigError(f"pairs[{i}] 引用了未定义消息类型: {mt}")
        result.append(PairSpec(producer=producer, consumer=consumer, message_types=list(mts)))
    return result
