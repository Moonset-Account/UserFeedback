"""内存关系数据库：表、行、外键由调用方建立，删除语句在此之上执行。"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

from .delete import DeleteResult, execute_delete
from .errors import DeleteBlockedError  # noqa: F401  （便于从包根导入）
from .schema import CASCADE, CHECK, ForeignKey, Table, normalize_key, normalize_pk


class Database:
    """一个极简的内存关系引擎。

    - 行以 dict 表示，按主键元组存放。
    - 列可在 insert 时按需注册。
    - 外键可跨表，也可指回本表（table == ref_table）。
    - 外键列的值为 None 时视为空值，不参与引用，也不触发任何策略。
    """

    def __init__(self) -> None:
        self.tables: Dict[str, Table] = {}
        self.foreign_keys: Dict[str, ForeignKey] = {}

    # ------------------------------------------------------------------ 模式

    def create_table(self, name: str, pk: Any) -> Table:
        if not isinstance(name, str) or not name:
            raise ValueError("table name must be a non-empty string")
        if name in self.tables:
            raise ValueError(f"table {name!r} already exists")
        table = Table(name, pk)
        self.tables[name] = table
        return table

    def add_foreign_key(
        self,
        name: str,
        table: str,
        columns: Any,
        ref_table: str,
        ref_columns: Any,
        on_delete: str,
    ) -> ForeignKey:
        if name in self.foreign_keys:
            raise ValueError(f"foreign key {name!r} already exists")
        if table not in self.tables:
            raise ValueError(f"unknown referencing table {table!r}")
        if ref_table not in self.tables:
            raise ValueError(f"unknown referenced table {ref_table!r}")
        if on_delete not in (CASCADE, CHECK):
            raise ValueError(f"on_delete must be {CASCADE!r} or {CHECK!r}")

        fk_table = self.tables[table]
        ref = self.tables[ref_table]
        fk_cols = (columns,) if isinstance(columns, str) else tuple(columns)
        ref_cols = (ref_columns,) if isinstance(ref_columns, str) else tuple(ref_columns)

        if not fk_cols or any(not isinstance(c, str) or not c for c in fk_cols):
            raise ValueError("foreign key columns must be non-empty strings")
        if len(fk_cols) != len(ref_cols):
            raise ValueError("foreign key and referenced column counts must match")
        if ref_cols != ref.pk_columns:
            # 本引擎只支持引用被引用表主键的完整集合。
            raise ValueError(
                f"foreign key {name!r} must reference the full primary key "
                f"{ref.pk_columns} of table {ref_table!r}"
            )
        for col in fk_cols:
            if col not in fk_table.columns:
                fk_table.columns.append(col)

        fk = ForeignKey(name, table, fk_cols, ref_table, ref_cols, on_delete)
        self.foreign_keys[name] = fk
        return fk

    def foreign_keys_from(self, table_name: str) -> List[ForeignKey]:
        return [fk for fk in self.foreign_keys.values() if fk.table == table_name]

    # ------------------------------------------------------------------ 数据

    def insert(self, table_name: str, row: Dict[str, Any]) -> Tuple[Any, ...]:
        table = self._require_table(table_name)
        if not isinstance(row, dict):
            raise TypeError("row must be a dict of column/value pairs")
        for col in row:
            if col not in table.columns:
                table.columns.append(col)
        full = {col: row.get(col) for col in table.columns}
        key = table.key_of(full)
        if key in table.rows:
            raise ValueError(
                f"duplicate primary key {key} already exists in table {table_name!r}"
            )
        table.rows[key] = full
        return key

    def get_row(self, table_name: str, key: Any) -> Dict[str, Any]:
        table = self._require_table(table_name)
        canonical = normalize_key(table.pk_columns, key)
        if canonical not in table.rows:
            raise KeyError(f"no row with key {canonical} in table {table_name!r}")
        return dict(table.rows[canonical])

    def count(self, table_name: str) -> int:
        return len(self._require_table(table_name).rows)

    # ---------------------------------------------------------------- 删除语句

    def delete_rows(
        self, targets: Iterable[Tuple[str, Any]]
    ) -> DeleteResult:
        """执行一条批量删除语句。

        targets 中的每一项为 (表名, 主键值)；单列主键直接传标量，
        复合主键必须传元组。同一条语句允许跨表混合指定。

        成功返回 DeleteResult；若语句末检查发现幸存引用，抛
        DeleteBlockedError 且库内数据保持语句开始前的状态。
        """
        normalized: List[Tuple[str, Tuple[Any, ...]]] = []
        for item in targets:
            table_name, raw_key = item
            table = self._require_table(table_name)
            normalized.append((table_name, normalize_key(table.pk_columns, raw_key)))
        return execute_delete(self, normalized)

    def delete(self, table_name: str, keys: Any) -> DeleteResult:
        """单表便捷入口：keys 可为单个键值，也可为键值的可迭代集合。"""
        table = self._require_table(table_name)
        if isinstance(keys, tuple):
            # 元组整体视为一个复合主键；批量场景请传列表。
            candidates = [keys]
        elif isinstance(keys, (list, set, frozenset)):
            candidates = list(keys)
        else:
            candidates = [keys]
        # 触发一次键值规范化以便尽早报错。
        for key in candidates:
            normalize_key(table.pk_columns, key)
        return self.delete_rows((table_name, key) for key in candidates)

    # ------------------------------------------------------------------ 内部

    def _require_table(self, table_name: str) -> Table:
        table = self.tables.get(table_name)
        if table is None:
            raise KeyError(f"unknown table {table_name!r}")
        return table
