"""值规范化与分组键编码。

本模块只负责两件事：

* 把调用方给出的分组键 / 聚合输入规范化成"参与相等比较与算术"的不可变值；
* 判定什么算"无值"（``None``）。

设计约定（见 README）：

* ``None`` 永远是"无值"。本包不引入任何哨兵对象去代表"小计空值"——
  小计里被省略的分组列同样是 ``None``，它与数据本身的 NULL 的区别
  完全由 :class:`~grouping_sets.model.ResultRow` 携带的 GROUPING 位图给出。
* 因此比较结果行时，位图是行身份的一部分，而 ``None`` 的含义由位图解释。
"""

from __future__ import annotations

import datetime
import decimal
from typing import Any

from .errors import NonFiniteValueError, UnhashableGroupingValueError


def normalize_value(value: Any) -> Any:
    """把单个值规范化成可哈希的不可变值。

    * ``None`` 原样保留（"无值"）；
    * ``str`` / ``int`` / ``bool`` / ``bytes`` / ``frozenset`` 原样保留；
    * 浮点 NaN、非有限 ``Decimal`` 抛 :class:`NonFiniteValueError`；
    * ``list`` / ``tuple`` → tuple（逐元素递归规范化）；
    * ``set`` → frozenset（逐元素递归规范化）；
    * ``dict`` → 按键的 repr 排序的 tuple（递归规范化），便于比较；
    * ``datetime`` / ``date`` / ``time`` 原样保留；
    * 其它可哈希对象原样保留；不可哈希且无已知规范化规则时抛
      :class:`UnhashableGroupingValueError`。

    注意：规范化不改变相等语义，仍遵循 Python 的 ``==``。
    例如 ``1 == Decimal("1")``，它们会落到同一个分组里。
    """

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, (int, bytes, frozenset)):
        return value
    if isinstance(value, float):
        if value != value:
            raise NonFiniteValueError("NaN cannot be used as a grouping value")
        return value
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            raise NonFiniteValueError(
                "non-finite Decimal cannot be used as a grouping value: %r" % (value,)
            )
        return value
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        # 均为 hashable；时区感知与 naive 值不相等，由调用方保证同质。
        return value
    if isinstance(value, (list, tuple)):
        return tuple(normalize_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(normalize_value(item) for item in value)
    if isinstance(value, dict):
        return tuple(
            sorted(
                ((normalize_value(k), normalize_value(v)) for k, v in value.items()),
                key=repr,
            )
        )
    try:
        hash(value)
    except TypeError as exc:  # 未预期的可变对象
        raise UnhashableGroupingValueError(
            "value %r is not hashable and has no known normalization" % (value,)
        ) from exc
    return value


def is_null(value: Any) -> bool:
    """聚合语义里的"无值"判定：只有 ``None`` 被视为无值。"""

    return value is None
