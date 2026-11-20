"""向后兼容检查：新读取模式 vs 保留期内的旧写入模式。

检查规则：
- 类型须逐层相容（int/int、string/string、记录递归、数组元素递归）。
- 旧字段可空而新字段不可空 -> 不兼容（旧消息中的显式空值须由新类型接纳）。
- 新字段在旧记录中无来源时，必须声明类型正确的缺省值（可空字段允许隐式 null）。
- 数组元素内部的不兼容同样记录完整路径（以 [] 标记元素层）。
问题按字段声明顺序、深度优先收集，列表首项即第一个不兼容路径。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from .resolution import ResolutionError, join_path, resolve_record_fields
from .types import ArrayType, Field, IntType, RecordType, SchemaType, StringType


@dataclass(frozen=True)
class Issue:
    path: str
    reason: str

    def __str__(self) -> str:
        return "%s: %s" % (self.path or "<root>", self.reason)


def validate_value(
    schema: SchemaType, value: Any, nullable: bool, path: str = ""
) -> Optional[str]:
    """校验值是否符合类型；返回 None 表示通过，否则返回原因。"""
    if value is None:
        return None if nullable else "不允许空值"
    if isinstance(schema, IntType):
        if isinstance(value, bool) or not isinstance(value, int):
            return "期望 int，实际 %s" % type(value).__name__
        return None
    if isinstance(schema, StringType):
        if not isinstance(value, str):
            return "期望 string，实际 %s" % type(value).__name__
        return None
    if isinstance(schema, ArrayType):
        if not isinstance(value, (list, tuple)):
            return "期望数组，实际 %s" % type(value).__name__
        for i, item in enumerate(value):
            err = validate_value(schema.element, item, False, "%s[%d]" % (path, i))
            if err:
                return "[%d] %s" % (i, err)
        return None
    if isinstance(schema, RecordType):
        if not isinstance(value, dict):
            return "期望对象，实际 %s" % type(value).__name__
        for f in schema.fields:
            if f.name in value:
                err = validate_value(
                    f.type, value[f.name], f.nullable, join_path(path, f.name)
                )
                if err:
                    return "%s: %s" % (f.name, err)
            elif f.has_default:
                err = validate_value(f.type, f.default, f.nullable, path)
                if err:
                    return "%s: 缺省值类型不正确: %s" % (f.name, err)
            elif not f.nullable:
                return "缺少字段 %s 且未声明缺省值" % f.name
        return None
    return "未知类型 %r" % (schema,)


def validate_declared_defaults(schema: SchemaType, path: str = "") -> List[Issue]:
    """登记前校验模式中所有已声明缺省值的类型正确性（含嵌套）。"""
    issues: List[Issue] = []
    if isinstance(schema, ArrayType):
        return validate_declared_defaults(schema.element, path + "[]")
    if not isinstance(schema, RecordType):
        return issues
    for f in schema.fields:
        fpath = join_path(path, f.name)
        if f.has_default:
            err = validate_value(f.type, f.default, f.nullable, fpath)
            if err:
                issues.append(Issue(fpath, "缺省值类型不正确: %s" % err))
        issues.extend(validate_declared_defaults(f.type, fpath))
    return issues


def check_type_compatible(
    new: SchemaType, old: SchemaType, path: str, issues: List[Issue]
) -> None:
    if isinstance(new, IntType) and isinstance(old, IntType):
        return
    if isinstance(new, StringType) and isinstance(old, StringType):
        return
    if isinstance(new, ArrayType) and isinstance(old, ArrayType):
        check_type_compatible(new.element, old.element, path + "[]", issues)
        return
    if isinstance(new, RecordType) and isinstance(old, RecordType):
        check_record_compatible(new, old, path, issues)
        return
    issues.append(
        Issue(path, "类型不兼容: 新 %s vs 旧 %s" % (new.describe(), old.describe()))
    )


def check_record_compatible(
    new: RecordType,
    old: RecordType,
    path: str = "",
    issues: Optional[List[Issue]] = None,
) -> List[Issue]:
    """比较一对记录模式，返回不兼容问题列表（首项为第一个不兼容路径）。"""
    if issues is None:
        issues = []
    try:
        mapping = resolve_record_fields(new, old, path)
    except ResolutionError as exc:
        issues.append(Issue(path, str(exc)))
        return issues
    for nf in new.fields:
        fpath = join_path(path, nf.name)
        of = mapping[nf.name]
        if of is None:
            if nf.has_default:
                err = validate_value(nf.type, nf.default, nf.nullable, fpath)
                if err:
                    issues.append(Issue(fpath, "缺省值类型不正确: %s" % err))
            elif not nf.nullable:
                issues.append(
                    Issue(fpath, "旧记录缺少对应字段，且新字段未声明缺省值")
                )
            continue
        if of.nullable and not nf.nullable:
            issues.append(
                Issue(fpath, "旧字段允许空值，新字段必须可空以接纳显式空值")
            )
        check_type_compatible(nf.type, of.type, fpath, issues)
    return issues
