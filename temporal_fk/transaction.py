"""事务：暂存修改并在提交边界核实时态外键。

同一事务内可以先删除旧的父区间、再插入若干相邻小段，并同时增改子记录。
提交时按**最新已确认的关系状态 + 本事务的暂存修改**合并出的最新关系
状态整体核对：

1. 先检查写冲突：本事务要改 / 删的旧行是否已被其他并发事务删除；
2. 再检查时态外键：每个父键下所有父记录有效期的并集，必须完整覆盖
   该键下每条子记录的整段半开有效期；
3. 全部通过后变更才原子写入存储，任一步失败都不改变已确认状态。
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Dict, List, Optional, Tuple, Type

from .coverage import evaluate_coverage
from .errors import (
    RowNotFoundError,
    TemporalForeignKeyError,
    TransactionClosedError,
    WriteConflictError,
)
from .models import Row

_UNSET = object()


class Transaction:
    def __init__(self, store: "TemporalStore"):  # noqa: F821 (前向引用)
        self._store = store
        self._active = True

        # 暂存区：插入的新行、删除的已确认行 id、替换旧行的更新版本
        self._parent_inserts: List[Row] = []
        self._parent_deletes: List[int] = []
        self._parent_updates: Dict[int, Row] = {}
        self._child_inserts: List[Row] = []
        self._child_deletes: List[int] = []
        self._child_updates: Dict[int, Row] = {}

    # ------------------------------------------------------------------
    # 基础保障
    # ------------------------------------------------------------------
    def _check_active(self) -> None:
        if not self._active:
            raise TransactionClosedError("transaction has already been committed or rolled back")

    @property
    def active(self) -> bool:
        return self._active

    # ------------------------------------------------------------------
    # 父记录操作
    # ------------------------------------------------------------------
    def insert_parent(self, key: Any, start: Any, end: Any) -> int:
        """插入一条父记录，返回分配到的行 id。"""
        self._check_active()
        with self._store.lock:
            self._store._validate_new_interval(start, end)
            row_id = self._store._allocate_row_id()
        self._parent_inserts.append(Row(row_id, key, start, end))
        return row_id

    def delete_parent(self, row_id: int) -> None:
        """删除一条父记录（可以是已确认行，也可以是本事务刚插入的行）。"""
        self._check_active()
        with self._store.lock:
            self._resolve_target(self._store._committed_parents(), row_id, "parent")
        if row_id in self._parent_updates:
            del self._parent_updates[row_id]
        if row_id not in self._parent_deletes:
            self._parent_deletes.append(row_id)

    def update_parent(
        self,
        row_id: int,
        *,
        key: Any = _UNSET,
        start: Any = _UNSET,
        end: Any = _UNSET,
    ) -> None:
        """修改父记录的父键或有效期，只更新显式给出的字段。"""
        self._check_active()
        with self._store.lock:
            current = self._resolve_current(
                self._store._committed_parents(),
                self._parent_inserts,
                self._parent_updates,
                self._parent_deletes,
                row_id,
                "parent",
            )
            new_key = current.key if key is _UNSET else key
            new_start = current.start if start is _UNSET else start
            new_end = current.end if end is _UNSET else end
            self._store._validate_new_interval(new_start, new_end)
        updated = Row(row_id, new_key, new_start, new_end)
        if row_id in {row.row_id for row in self._parent_inserts}:
            # 本事务插入的行：直接改插入暂存，不进入更新表
            self._parent_inserts = [
                updated if row.row_id == row_id else row for row in self._parent_inserts
            ]
        else:
            self._parent_updates[row_id] = updated

    # ------------------------------------------------------------------
    # 子记录操作
    # ------------------------------------------------------------------
    def insert_child(self, key: Any, start: Any, end: Any) -> int:
        """插入一条子记录，返回分配到的行 id。"""
        self._check_active()
        with self._store.lock:
            self._store._validate_new_interval(start, end)
            row_id = self._store._allocate_row_id()
        self._child_inserts.append(Row(row_id, key, start, end))
        return row_id

    def delete_child(self, row_id: int) -> None:
        """删除一条子记录。"""
        self._check_active()
        with self._store.lock:
            self._resolve_target(self._store._committed_children(), row_id, "child")
        if row_id in self._child_updates:
            del self._child_updates[row_id]
        if row_id not in self._child_deletes:
            self._child_deletes.append(row_id)

    def update_child(
        self,
        row_id: int,
        *,
        key: Any = _UNSET,
        start: Any = _UNSET,
        end: Any = _UNSET,
    ) -> None:
        """修改子记录的父键或有效期，只更新显式给出的字段。"""
        self._check_active()
        with self._store.lock:
            current = self._resolve_current(
                self._store._committed_children(),
                self._child_inserts,
                self._child_updates,
                self._child_deletes,
                row_id,
                "child",
            )
            new_key = current.key if key is _UNSET else key
            new_start = current.start if start is _UNSET else start
            new_end = current.end if end is _UNSET else end
            self._store._validate_new_interval(new_start, new_end)
        updated = Row(row_id, new_key, new_start, new_end)
        if row_id in {row.row_id for row in self._child_inserts}:
            self._child_inserts = [
                updated if row.row_id == row_id else row for row in self._child_inserts
            ]
        else:
            self._child_updates[row_id] = updated

    # ------------------------------------------------------------------
    # 行定位与写冲突检查
    # ------------------------------------------------------------------
    def _resolve_target(self, committed: Dict[int, Row], row_id: int, kind: str) -> None:
        """确认删除目标仍存在：在已确认状态或本事务插入集中，且未被本事务删除。"""
        if row_id in self._matching_deletes(kind):
            raise RowNotFoundError(f"{kind} row {row_id} was already deleted in this transaction")
        if row_id in committed:
            return
        if any(row.row_id == row_id for row in self._matching_inserts(kind)):
            return
        raise RowNotFoundError(f"{kind} row {row_id} does not exist")

    def _resolve_current(
        self,
        committed: Dict[int, Row],
        inserts: List[Row],
        updates: Dict[int, Row],
        deletes: List[int],
        row_id: int,
        kind: str,
    ) -> Row:
        """取一行在本事务视角下的最新版本。"""
        if row_id in deletes:
            raise RowNotFoundError(f"{kind} row {row_id} was already deleted in this transaction")
        for row in inserts:
            if row.row_id == row_id:
                return row
        if row_id in updates:
            return updates[row_id]
        row = committed.get(row_id)
        if row is None:
            raise RowNotFoundError(f"{kind} row {row_id} does not exist")
        return row

    def _matching_inserts(self, kind: str) -> List[Row]:
        return self._parent_inserts if kind == "parent" else self._child_inserts

    def _matching_deletes(self, kind: str) -> List[int]:
        return self._parent_deletes if kind == "parent" else self._child_deletes

    # ------------------------------------------------------------------
    # 提交 / 回滚
    # ------------------------------------------------------------------
    def commit(self) -> None:
        """在提交边界核对写冲突与时态外键，通过后原子生效。"""
        self._check_active()
        try:
            with self._store.lock:
                self._check_write_conflicts()
                effective_parents, effective_children = self._build_effective_state()
                gaps = evaluate_coverage(
                    self._group_by_key(effective_parents),
                    [
                        (row.row_id, row.key, row.interval)
                        for row in effective_children
                    ],
                )
                if gaps:
                    raise TemporalForeignKeyError(gaps)
                self._store._apply(
                    self._parent_inserts,
                    self._parent_deletes,
                    self._parent_updates,
                    self._child_inserts,
                    self._child_deletes,
                    self._child_updates,
                )
        finally:
            # 无论成功还是失败，事务都结束：失败的事务不允许继续使用
            self._active = False

    def rollback(self) -> None:
        """丢弃全部暂存修改。"""
        self._check_active()
        self._parent_inserts.clear()
        self._parent_deletes.clear()
        self._parent_updates.clear()
        self._child_inserts.clear()
        self._child_deletes.clear()
        self._child_updates.clear()
        self._active = False

    # ------------------------------------------------------------------
    # 提交边界的内部计算
    # ------------------------------------------------------------------
    def _check_write_conflicts(self) -> None:
        """本事务修改的已确认行，必须仍存在于最新已确认状态。"""
        committed_parents = self._store._committed_parents()
        committed_children = self._store._committed_children()
        local_parent_inserts = {row.row_id for row in self._parent_inserts}
        local_child_inserts = {row.row_id for row in self._child_inserts}

        stale: List[int] = []
        for row_id in (*self._parent_deletes, *self._parent_updates):
            if row_id not in local_parent_inserts and row_id not in committed_parents:
                stale.append(row_id)
        for row_id in (*self._child_deletes, *self._child_updates):
            if row_id not in local_child_inserts and row_id not in committed_children:
                stale.append(row_id)
        if stale:
            raise WriteConflictError(
                f"rows deleted by a concurrently committed transaction: {sorted(stale)}"
            )

    def _build_effective_state(self) -> Tuple[List[Row], List[Row]]:
        """合并最新已确认状态与本事务暂存，得到提交后的完整关系状态。"""
        parents = self._store._committed_parents()
        children = self._store._committed_children()

        for row_id in self._parent_deletes:
            parents.pop(row_id, None)
        parents.update(self._parent_updates)
        for row in self._parent_inserts:
            # 同事务插入后又删除的行：既不应生效也不参与核对
            if row.row_id not in self._parent_deletes:
                parents[row.row_id] = row

        for row_id in self._child_deletes:
            children.pop(row_id, None)
        children.update(self._child_updates)
        for row in self._child_inserts:
            if row.row_id not in self._child_deletes:
                children[row.row_id] = row

        return list(parents.values()), list(children.values())

    @staticmethod
    def _group_by_key(rows: List[Row]) -> Dict[Any, List[Tuple[Any, Any]]]:
        grouped: Dict[Any, List[Tuple[Any, Any]]] = {}
        for row in rows:
            grouped.setdefault(row.key, []).append(row.interval)
        return grouped

    # ------------------------------------------------------------------
    # 上下文管理支持
    # ------------------------------------------------------------------
    def __enter__(self) -> "Transaction":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        if self._active:
            # 正常退出（无异常）时提交；with 块抛异常则回滚
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
