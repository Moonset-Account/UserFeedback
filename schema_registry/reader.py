"""按兼容检查通过的同一套规则读取旧消息。

解析规则与 schema_registry.resolution 完全一致：
原名优先、别名唯一命中、旧字段不可重复分配；
缺字段才使用缺省值；显式空值仅可空字段接纳。
"""
from __future__ import annotations

from typing import Any

from .resolution import ResolutionError, join_path, resolve_record_fields
from .types import ArrayType, IntType, RecordType, SchemaType, StringType


class ReadError(Exception):
    """读取旧消息失败（结构不符或空值不被接纳等）。"""


def read_message(new_schema: RecordType, old_schema: RecordType, message: Any) -> dict:
    if not isinstance(new_schema, RecordType) or not isinstance(old_schema, RecordType):
        raise ReadError("顶层模式必须是记录")
    return _read_record(new_schema, old_schema, message, "")


def _read_record(
    new_rec: RecordType, old_rec: RecordType, data: Any, path: str
) -> dict:
    if not isinstance(data, dict):
        raise ReadError(
            "%s: 期望对象，实际 %s" % (path or "<root>", type(data).__name__)
        )
    try:
        mapping = resolve_record_fields(new_rec, old_rec, path)
    except ResolutionError as exc:
        raise ReadError(str(exc)) from exc
    out = {}
    for nf in new_rec.fields:
        fpath = join_path(path, nf.name)
        of = mapping[nf.name]
        if of is not None and of.name in data:
            out[nf.name] = _read_value(
                nf.type, of.type, data[of.name], nf.nullable, fpath
            )
        elif nf.has_default:
            out[nf.name] = nf.default
        elif nf.nullable:
            out[nf.name] = None
        else:
            raise ReadError("%s: 旧消息缺少字段且新字段未声明缺省值" % fpath)
    return out


def _read_value(
    new_t: SchemaType, old_t: SchemaType, value: Any, nullable: bool, path: str
) -> Any:
    if value is None:
        if nullable:
            return None
        raise ReadError("%s: 新类型不接纳旧消息中的显式空值" % path)
    if isinstance(new_t, IntType):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ReadError("%s: 期望 int，实际 %s" % (path, type(value).__name__))
        return value
    if isinstance(new_t, StringType):
        if not isinstance(value, str):
            raise ReadError(
                "%s: 期望 string，实际 %s" % (path, type(value).__name__)
            )
        return value
    if isinstance(new_t, ArrayType):
        if not isinstance(old_t, ArrayType):
            raise ReadError("%s: 旧字段不是数组" % path)
        if not isinstance(value, (list, tuple)):
            raise ReadError("%s: 期望数组，实际 %s" % (path, type(value).__name__))
        return [
            _read_value(new_t.element, old_t.element, item, False, "%s[%d]" % (path, i))
            for i, item in enumerate(value)
        ]
    if isinstance(new_t, RecordType):
        if not isinstance(old_t, RecordType):
            raise ReadError("%s: 旧字段不是记录" % path)
        return _read_record(new_t, old_t, value, path)
    raise ReadError("%s: 未知类型 %r" % (path, new_t))
