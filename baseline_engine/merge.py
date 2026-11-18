"""分层配置合并：平台默认值 -> 服务定义 -> 覆盖层。

标量字段采用“显式给值即覆盖上层”的规则；列表字段通过 :class:`ListOp`
表达合并（merge）、清除替换（replace）、剔除（remove）三种语义。
"""

from __future__ import annotations

from typing import Any, Mapping

from .models import ListOp

_LIST_FIELDS = ("write_paths", "forbidden_identities")
_SCALAR_FIELDS = ("user", "group", "low_privilege_required", "description")


def apply_list_op(base: list[Any], op: ListOp) -> list[Any]:
    """按覆盖操作对上层列表求值。"""
    if op.op == ListOp.REPLACE:
        # 显式清除上层列表
        return list(op.items)
    if op.op == ListOp.REMOVE:
        removed = set(_hashable_key(x) for x in op.items)
        return [item for item in base if _hashable_key(item) not in removed]
    # merge：保序去重
    merged = list(base)
    seen = {_hashable_key(x) for x in merged}
    for item in op.items:
        key = _hashable_key(item)
        if key not in seen:
            merged.append(item)
            seen.add(key)
    return merged


def _hashable_key(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return ("__list__", tuple(_hashable_key(v) for v in value))
    if isinstance(value, Mapping):
        return (
            "__dict__",
            tuple(sorted((k, _hashable_key(v)) for k, v in value.items())),
        )
    return value


def _as_list_op(value: Any) -> ListOp | None:
    if value is None:
        return None
    if isinstance(value, ListOp):
        return value
    if isinstance(value, list):
        return ListOp(ListOp.MERGE, value)
    if isinstance(value, (Mapping, str)):
        return ListOp.coerce(value)
    raise TypeError(f"无法解释的列表字段值: {value!r}")


def merge_layers(layers: list[Mapping[str, Any]]) -> dict[str, Any]:
    """按顺序叠加多个配置层，返回最终配置字典。

    每层仅覆盖自己显式给出的字段；列表字段按 ListOp 语义处理。
    """
    resolved: dict[str, Any] = {
        "user": None,
        "group": None,
        "write_paths": [],
        "forbidden_identities": [],
        "low_privilege_required": None,
        "description": "",
    }
    for layer in layers:
        for field_name in _SCALAR_FIELDS:
            if field_name in layer and layer[field_name] is not None:
                resolved[field_name] = layer[field_name]
        for field_name in _LIST_FIELDS:
            if field_name not in layer:
                continue
            op = _as_list_op(layer[field_name])
            if op is not None:
                resolved[field_name] = apply_list_op(
                    list(resolved[field_name]), op
                )
    return resolved
