"""时态关系存储与事务管理。

存储维护两张表（父记录、子记录），每张表是 ``row_id -> Row`` 的字典。
所有修改都通过 :class:`~temporal_fk.transaction.Transaction` 暂存，
在提交边界一次性核对时态外键并原子生效。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Tuple

from .errors import RowNotFoundError
from .intervals import validate_interval
from .models import Row
from .transaction import Transaction


class TemporalStore:
    """线程安全的内存时态关系存储。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._parents: Dict[int, Row] = {}
        self._children: Dict[int, Row] = {}
        self._next_row_id = 1

    # ------------------------------------------------------------------
    # 事务入口
    # ------------------------------------------------------------------
    def begin(self) -> Transaction:
        """开启一个新事务。"""
        return Transaction(self)

    # ------------------------------------------------------------------
    # 供事务调用的内部接口（均持锁调用）
    # ------------------------------------------------------------------
    def _allocate_row_id(self) -> int:
        row_id = self._next_row_id
        self._next_row_id += 1
        return row_id

    def _committed_parents(self) -> Dict[int, Row]:
        return dict(self._parents)

    def _committed_children(self) -> Dict[int, Row]:
        return dict(self._children)

    def _validate_new_interval(self, start: Any, end: Any) -> None:
        validate_interval(start, end)

    def _require_existing_row(self, table: Dict[int, Row], row_id: int) -> Row:
        row = table.get(row_id)
        if row is None:
            raise RowNotFoundError(f"row {row_id} does not exist in committed state")
        return row

    def _apply(
        self,
        parent_inserts: List[Row],
        parent_deletes: List[int],
        parent_updates: Dict[int, Row],
        child_inserts: List[Row],
        child_deletes: List[int],
        child_updates: Dict[int, Row],
    ) -> None:
        """核对通过后把事务变更原子写入已确认状态。"""
        for row_id in parent_deletes:
            self._parents.pop(row_id, None)
        self._parents.update(parent_updates)
        for row in parent_inserts:
            # 同事务插入后又删除的行不生效
            if row.row_id not in parent_deletes:
                self._parents[row.row_id] = row

        for row_id in child_deletes:
            self._children.pop(row_id, None)
        self._children.update(child_updates)
        for row in child_inserts:
            if row.row_id not in child_deletes:
                self._children[row.row_id] = row

    # ------------------------------------------------------------------
    # 只读检查接口（测试 / 调试用，返回拷贝，避免外部绕过事务修改）
    # ------------------------------------------------------------------
    def get_parent(self, row_id: int) -> Row:
        with self._lock:
            return self._require_existing_row(self._parents, row_id)

    def get_child(self, row_id: int) -> Row:
        with self._lock:
            return self._require_existing_row(self._children, row_id)

    def list_parents(self) -> List[Row]:
        with self._lock:
            return [self._parents[k] for k in sorted(self._parents)]

    def list_children(self) -> List[Row]:
        with self._lock:
            return [self._children[k] for k in sorted(self._children)]

    def parent_intervals(self, key: Any) -> List[Tuple[Any, Any]]:
        """已确认状态下某个父键的全部父记录有效期。"""
        with self._lock:
            return [row.interval for row in self._parents.values() if row.key == key]

    def child_intervals(self, key: Any) -> List[Tuple[Any, Any]]:
        """已确认状态下某个父键的全部子记录有效期。"""
        with self._lock:
            return [row.interval for row in self._children.values() if row.key == key]

    @property
    def lock(self) -> threading.RLock:
        return self._lock
