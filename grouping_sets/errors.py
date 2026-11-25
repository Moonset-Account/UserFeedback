"""异常类型。

全部继承 :class:`GroupingSetsError`（本身是 ``ValueError``），
便于调用方一把捕获所有"调用方输入不合法"的错误。
"""

from __future__ import annotations


class GroupingSetsError(ValueError):
    """本包所有错误的基类。"""


class SchemaError(GroupingSetsError):
    """分组列 / 分组集合 / 聚合列的定义非法。"""


class MissingColumnError(GroupingSetsError):
    """输入行缺少某个已声明的列。"""


class UnknownColumnError(GroupingSetsError):
    """分组集合或聚合列引用了未声明的列。"""


class DuplicateColumnError(GroupingSetsError):
    """同一个分组集合里重复列出了同一列，或聚合列重复。"""


class UnmatchedRetractionError(GroupingSetsError):
    """严格模式下撤回了状态中不存在的一行。"""


class NonFiniteValueError(GroupingSetsError):
    """NaN / inf 出现在分组键或聚合输入中。

    它们既不等于自己（NaN），也会让最小值和求和失去良定义的比较结果，
    因此本包在入口处拒绝，而不是悄悄传播到输出。
    """


class UnhashableGroupingValueError(GroupingSetsError):
    """分组值无法规范化成可哈希的不可变值。"""


# 兼容旧名字
UnhashableGroupingValue = UnhashableGroupingValueError


class AggregateTypeError(GroupingSetsError):
    """聚合列里混入了无法一起求和的类型（例如 float 与 Decimal）。"""
