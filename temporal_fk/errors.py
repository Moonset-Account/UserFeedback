"""时态外键原型的异常类型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Tuple


class TemporalStoreError(Exception):
    """所有存储层错误的基类。"""


class TransactionClosedError(TemporalStoreError):
    """事务已经提交或回滚后再次使用时抛出。"""


class RowNotFoundError(TemporalStoreError):
    """更新 / 删除的行在最新已确认状态中不存在时抛出。"""


class WriteConflictError(TemporalStoreError):
    """事务基于过期的已确认状态修改了已被其他事务删除的行。"""


@dataclass(frozen=True)
class ChildCoverageGap:
    """一条子记录缺失覆盖的具体信息。

    :param child_row_id: 失去覆盖的子记录行 id
    :param key: 子记录携带的父键
    :param child_interval: 子记录的完整有效期
    :param missing: 实际缺掉的有序、不相交半开区间列表
    """

    child_row_id: int
    key: Any
    child_interval: Tuple[Any, Any]
    missing: Tuple[Tuple[Any, Any], ...]


class TemporalForeignKeyError(TemporalStoreError):
    """提交时时态外键约束失败，携带所有失去覆盖的子区间。"""

    def __init__(self, gaps: list[ChildCoverageGap]):
        self.gaps = gaps
        detail = "; ".join(
            f"child row {gap.child_row_id} key={gap.key!r} "
            f"interval=[{gap.child_interval[0]}, {gap.child_interval[1]}) "
            f"missing={[list(part) for part in gap.missing]}"
            for gap in gaps
        )
        super().__init__(f"temporal foreign key violation: {detail}")
