"""批量删除语句的执行核心。

执行分三个阶段，保证题面要求的语义：

1. 计划阶段：把显式删除目标和所有 CASCADE 外键触发的级联目标求传递闭包，
   显式删除与级联删除同属一条语句；planned 集合保证两个表互相级联、
   或同一行经两条路径到达时只被计划一次。
2. 语句末检查：闭包完成后，针对每条 CHECK 外键，检查“幸存行”（不在删除
   集合中的行）是否仍引用删除集合中的目标；发现任何阻塞即抛
   DeleteBlockedError，此时尚未物理删除任何行，整条语句自动回退。
3. 落盘阶段：检查全部通过后才真正从各表移除行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from .errors import BlockedReference, DeleteBlockedError
from .schema import CASCADE, CHECK, ForeignKey, Table

TargetKey = Tuple[str, Tuple[Any, ...]]


@dataclass(frozen=True)
class DeletedRow:
    table: str
    key: Tuple[Any, ...]
    row: Dict[str, Any]


class DeleteResult:
    """成功语句的删除结果。"""

    def __init__(
        self,
        deleted: List[DeletedRow],
        missing: List[TargetKey],
    ) -> None:
        self.deleted: Tuple[DeletedRow, ...] = tuple(deleted)
        # 显式指定但原本就不存在的目标（不计入删除，也不报错）。
        self.missing: Tuple[TargetKey, ...] = tuple(missing)

    @property
    def count(self) -> int:
        return len(self.deleted)

    def was_deleted(self, table_name: str, key: Tuple[Any, ...]) -> bool:
        return any(d.table == table_name and d.key == tuple(key) for d in self.deleted)

    def keys_by_table(self, table_name: str) -> List[Tuple[Any, ...]]:
        return [d.key for d in self.deleted if d.table == table_name]

    def __repr__(self) -> str:  # pragma: no cover - 仅便于调试
        return f"DeleteResult(count={self.count}, missing={list(self.missing)})"


class _FkIndexes:
    """为单条语句按需构建的反向引用索引：外键值 -> 引用它的行键列表。

    外键列含 None 的行视为空引用，不建索引、不参与任何策略。
    """

    def __init__(self, db) -> None:
        self._db = db
        # fk.name -> {外键值元组: [引用方行键, ...]}
        self._by_fk: Dict[str, Dict[Tuple[Any, ...], List[Tuple[Any, ...]]]] = {}

    def referencing_keys(
        self, fk: ForeignKey, fk_value: Tuple[Any, ...]
    ) -> List[Tuple[Any, ...]]:
        index = self._by_fk.get(fk.name)
        if index is None:
            table: Table = self._db.tables[fk.table]
            index = {}
            for row_key, row in table.rows.items():
                value = tuple(row.get(col) for col in fk.columns)
                if any(part is None for part in value):
                    continue  # 空外键不引用任何目标
                index.setdefault(value, []).append(row_key)
            self._by_fk[fk.name] = index
        return index.get(fk_value, ())


def execute_delete(db, targets: List[TargetKey]) -> DeleteResult:
    indexes = _FkIndexes(db)

    # ------------------------------------------------------------ 阶段 1：计划

    planned: set[TargetKey] = set()
    missing_set: set[TargetKey] = set()
    missing: List[TargetKey] = []
    work: List[TargetKey] = []

    for table_name, key in targets:
        target = (table_name, key)
        if target in planned or target in missing_set:
            continue  # 同一显式目标重复指定只算一次（含不存在的目标）
        table = db.tables[table_name]
        if key not in table.rows:
            missing_set.add(target)
            missing.append(target)
            continue
        planned.add(target)
        work.append(target)

    while work:
        cur_table_name, cur_key = work.pop()
        current_table: Table = db.tables[cur_table_name]
        current_row = current_table.rows[cur_key]

        for fk in db.foreign_keys.values():
            if fk.ref_table != cur_table_name or fk.on_delete != CASCADE:
                # CHECK 策略不在此级联：幸存引用留待阶段 2 统一判定。
                continue
            # 被删目标在外键引用列上的值，即引用方外键需要匹配的值。
            referenced_value = tuple(current_row[col] for col in fk.ref_columns)
            for ref_row_key in indexes.referencing_keys(fk, referenced_value):
                child = (fk.table, ref_row_key)
                if child in planned:
                    continue  # 互级联 / 双路径：只计划一次
                planned.add(child)
                work.append(child)

    # ------------------------------------------------------- 阶段 2：语句末检查

    violations: List[BlockedReference] = []

    for fk in db.foreign_keys.values():
        if fk.on_delete != CHECK:
            continue
        referenced_table: Table = db.tables[fk.ref_table]
        referencing_table: Table = db.tables[fk.table]

        deleted_targets = sorted(
            (t for t in planned if t[0] == fk.ref_table),
            key=lambda t: repr(t[1]),
        )
        for target_table_name, target_key in deleted_targets:
            target_row = referenced_table.rows[target_key]
            referenced_value = tuple(target_row[col] for col in fk.ref_columns)

            for ref_row_key in indexes.referencing_keys(fk, referenced_value):
                if (fk.table, ref_row_key) in planned:
                    continue  # 引用方也在本语句中被删，引用随其消失，不阻塞
                violations.append(
                    BlockedReference(
                        foreign_key=fk.name,
                        referencing_table=fk.table,
                        referencing_key=ref_row_key,
                        referencing_row=dict(referencing_table.rows[ref_row_key]),
                        referenced_table=target_table_name,
                        referenced_key=target_key,
                        referenced_row=dict(target_row),
                    )
                )

    if violations:
        # 确定性顺序：按引用方表/键、再按目标表/键排列，方便调用方断言。
        violations.sort(
            key=lambda v: (
                v.referencing_table,
                repr(v.referencing_key),
                v.referenced_table,
                repr(v.referenced_key),
            )
        )
        # 尚未修改任何表行，抛出即等价于整条语句回退。
        raise DeleteBlockedError(violations)

    # ------------------------------------------------------------ 阶段 3：落盘

    deleted_rows: List[DeletedRow] = []
    for table_name, key in planned:
        table = db.tables[table_name]
        row = table.rows.pop(key)
        deleted_rows.append(DeletedRow(table_name, key, row))

    deleted_rows.sort(key=lambda d: (d.table, repr(d.key)))
    return DeleteResult(deleted_rows, missing)
