"""时态外键关系存储原型。

公开 API：

* :class:`TemporalStore` / :class:`Transaction`：存储与事务
* :class:`Row`：父 / 子记录行
* :func:`evaluate_coverage`：纯函数式覆盖核对
* :func:`merge_intervals` / :func:`uncovered_parts` / :func:`validate_interval`：
  半开区间工具
* 异常类型：:class:`TemporalStoreError`、:class:`TemporalForeignKeyError`、
  :class:`ChildCoverageGap`、:class:`WriteConflictError`、
  :class:`RowNotFoundError`、:class:`TransactionClosedError`
"""

from .coverage import evaluate_coverage
from .errors import (
    ChildCoverageGap,
    RowNotFoundError,
    TemporalForeignKeyError,
    TemporalStoreError,
    TransactionClosedError,
    WriteConflictError,
)
from .intervals import merge_intervals, uncovered_parts, validate_interval
from .models import Row
from .store import TemporalStore
from .transaction import Transaction

__all__ = [
    "TemporalStore",
    "Transaction",
    "Row",
    "evaluate_coverage",
    "merge_intervals",
    "uncovered_parts",
    "validate_interval",
    "TemporalStoreError",
    "TemporalForeignKeyError",
    "ChildCoverageGap",
    "WriteConflictError",
    "RowNotFoundError",
    "TransactionClosedError",
]
