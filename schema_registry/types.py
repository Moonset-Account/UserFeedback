"""消息模式类型系统。

支持的类型：
- 整数 IntType
- 字符串 StringType
- 数组 ArrayType（元素类型可嵌套）
- 具名记录 RecordType（字段可嵌套记录或数组）

字段 Field 可声明：
- nullable: 是否允许空值
- aliases: 旧名（读取旧消息时的候选来源名）
- default: 缺字段时使用的缺省值
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, Union


class _Missing:
    """缺省值未声明时使用的哨兵，区别于显式的 None。"""

    __slots__ = ()
    _instance: Optional["_Missing"] = None

    def __new__(cls) -> "_Missing":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


@dataclass(frozen=True)
class IntType:
    def describe(self) -> str:
        return "int"


@dataclass(frozen=True)
class StringType:
    def describe(self) -> str:
        return "string"


@dataclass(frozen=True)
class ArrayType:
    element: "SchemaType"

    def describe(self) -> str:
        return "array<%s>" % self.element.describe()


@dataclass(frozen=True)
class Field:
    name: str
    type: "SchemaType"
    nullable: bool = False
    aliases: Tuple[str, ...] = ()
    default: Any = MISSING

    @property
    def has_default(self) -> bool:
        return self.default is not MISSING


@dataclass(frozen=True)
class RecordType:
    name: str
    fields: Tuple[Field, ...] = ()

    def describe(self) -> str:
        return "record<%s>" % self.name

    def field_map(self) -> Dict[str, Field]:
        result: Dict[str, Field] = {}
        for f in self.fields:
            if f.name in result:
                raise ValueError("记录 %s 中字段名重复: %s" % (self.name, f.name))
            result[f.name] = f
        return result


SchemaType = Union[IntType, StringType, ArrayType, RecordType]

INT = IntType()
STRING = StringType()
