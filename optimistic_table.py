"""内存关系表的乐观提交（乐观并发控制）。

语义：
- 事务始终读取**开始快照**以及**自己的修改**（快照隔离 + read-your-writes）。
- 提交时做乐观校验，针对快照建立之后（version > 快照版本）的每一次外部提交，
  按该提交的**净效果**（before/after 镜像）检查：
  1. 写-写冲突：本事务要写的主键已被该外部提交写入（含删除）。
  2. 范围读冲突：该外部提交的净效果改变了本事务已登记范围读
     ``[lo, hi)`` 内的 (主键, 分值) 集合 —— 行进入/离开范围、
     范围内分值变化、范围内行被删除/插入，均算冲突；
     仅改写其他列不改变范围结果，不算冲突。
- 初读为空同样登记（防幻影）；范围外的分值变化不冲突，可并行提交。
- 冲突时抛出 :class:`ConflictError`，指出冲突的提交版本与主键。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


class ConflictError(Exception):
    """提交被拒绝。携带冲突提交版本、主键与原因。"""

    def __init__(self, commit_version: int, pk: Any, reason: str) -> None:
        self.commit_version = commit_version
        self.pk = pk
        self.reason = reason
        super().__init__(
            f"commit rejected: {reason} conflict with commit "
            f"v{commit_version} on primary key {pk!r}"
        )


class TransactionStateError(Exception):
    """事务状态非法（如已提交/已中止后继续使用）。"""


_DELETE = object()  # 删除标记


def _range_image(score: Any, lo: float, hi: float) -> Optional[Any]:
    """分值在范围 [lo, hi) 内的映像：在范围内返回分值本身，否则返回 None。

    两次映像不同即表示 (主键, 分值) 范围集合被净改变。
    """
    if score is None:
        return None
    return score if lo <= score < hi else None


@dataclass
class _CommitRecord:
    """一次已提交写入的净效果：涉及主键的 before/after 行镜像（None 表示不存在）。"""

    version: int
    before: Dict[Any, Optional[Dict[str, Any]]]
    after: Dict[Any, Optional[Dict[str, Any]]]


class Table:
    """内存关系表：主键 -> 列字典。通过 :meth:`begin` 开启事务。"""

    def __init__(self) -> None:
        self._rows: Dict[Any, Dict[str, Any]] = {}
        self._version = 0
        self._history: List[_CommitRecord] = []

    # ------------------------------------------------------------------ API
    def begin(self) -> "Transaction":
        """开启事务，捕获当前版本与快照。"""
        return Transaction(self)

    @property
    def version(self) -> int:
        return self._version

    def snapshot_rows(self) -> Dict[Any, Dict[str, Any]]:
        """返回当前已提交数据的拷贝（便于测试/检查）。"""
        return {pk: dict(row) for pk, row in self._rows.items()}

    # ------------------------------------------------------------- internal
    def _commit(self, tx: "Transaction") -> int:
        self._validate(tx)
        if not tx._writes:
            return self._version  # 只读事务：校验通过即可，不产生新版本

        before: Dict[Any, Optional[Dict[str, Any]]] = {}
        after: Dict[Any, Optional[Dict[str, Any]]] = {}
        for pk, change in tx._writes.items():
            old = self._rows.get(pk)
            before[pk] = dict(old) if old is not None else None
            if change is _DELETE:
                self._rows.pop(pk, None)
                after[pk] = None
            else:
                merged = dict(old) if old is not None else {}
                merged.update(change)
                self._rows[pk] = merged
                after[pk] = dict(merged)

        self._version += 1
        self._history.append(_CommitRecord(self._version, before, after))
        return self._version

    def _validate(self, tx: "Transaction") -> None:
        for rec in self._history:
            if rec.version <= tx._snapshot_version:
                continue  # 快照建立时已存在的提交不参与校验

            # 1) 写-写冲突：本事务要写的主键被该外部提交写过
            for pk in tx._writes:
                if pk in rec.after:
                    raise ConflictError(rec.version, pk, "write-write")

            # 2) 范围读冲突：该提交的净效果改变了已读范围的 (主键, 分值) 集合
            for lo, hi, column in tx._range_reads:
                for pk in rec.before.keys() | rec.after.keys():
                    b_row = rec.before.get(pk)
                    a_row = rec.after.get(pk)
                    b_img = _range_image(
                        b_row.get(column) if b_row is not None else None, lo, hi
                    )
                    a_img = _range_image(
                        a_row.get(column) if a_row is not None else None, lo, hi
                    )
                    if b_img != a_img:
                        raise ConflictError(rec.version, pk, "read-range")


class Transaction:
    """乐观事务：读开始快照与自己的修改，提交时统一校验。"""

    def __init__(self, table: Table) -> None:
        self._table = table
        self._snapshot_version = table._version
        self._snapshot: Dict[Any, Dict[str, Any]] = {
            pk: dict(row) for pk, row in table._rows.items()
        }
        self._writes: Dict[Any, Any] = {}  # pk -> 列更新字典 或 _DELETE
        self._range_reads: List[Tuple[float, float, str]] = []
        self._state = "active"

    # ------------------------------------------------------------------ API
    @property
    def state(self) -> str:
        return self._state

    def read_range(
        self, lo: float, hi: float, column: str = "score"
    ) -> List[Tuple[Any, Dict[str, Any]]]:
        """读取分值列落在 [lo, hi) 的所有行，并登记该范围读（含空结果）。

        返回按主键排序的 (主键, 行拷贝) 列表；内容为开始快照叠加自己的修改。
        """
        self._ensure_active()
        merged = dict(self._snapshot)
        for pk, change in self._writes.items():
            if change is _DELETE:
                merged.pop(pk, None)
            else:
                row = dict(merged.get(pk) or {})
                row.update(change)
                merged[pk] = row

        result = [
            (pk, dict(row))
            for pk, row in merged.items()
            if (score := row.get(column)) is not None and lo <= score < hi
        ]
        result.sort(key=lambda item: item[0])
        self._range_reads.append((lo, hi, column))
        return result

    def write(self, pk: Any, **columns: Any) -> None:
        """按主键 upsert 列更新（行不存在则创建）。"""
        self._ensure_active()
        pending = self._writes.get(pk)
        base = {} if pending is None or pending is _DELETE else dict(pending)
        base.update(columns)
        self._writes[pk] = base

    def delete(self, pk: Any) -> None:
        """按主键删除行。"""
        self._ensure_active()
        self._writes[pk] = _DELETE

    def commit(self) -> int:
        """校验并提交；冲突时抛 :class:`ConflictError` 并中止本事务。"""
        self._ensure_active()
        try:
            version = self._table._commit(self)
        except ConflictError:
            self._state = "aborted"
            raise
        self._state = "committed"
        return version

    # ------------------------------------------------------------- internal
    def _ensure_active(self) -> None:
        if self._state != "active":
            raise TransactionStateError(f"transaction is {self._state}")
