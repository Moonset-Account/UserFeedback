"""语句级约束检查器。

规划器每产生一条 INSERT / UPDATE 就调用一次检查器；测试也可以直接用它
验证手工构造的语句会被拒绝。检查器内部维护“已装载行”和“尚未补写的
暂缓外键”两份状态。
"""

from __future__ import annotations

from .errors import ConstraintViolationError
from .model import (
    ForeignKeySpec,
    Insert,
    TableSpec,
    Update,
)

Node = tuple[str, tuple]


class ConstraintChecker:
    """在模拟数据库上逐条执行装载语句并检查约束。"""

    def __init__(
        self,
        tables: dict[str, TableSpec],
        existing_keys: dict[str, set[tuple]] | None = None,
    ):
        self.tables = tables
        self.existing: dict[str, set[tuple]] = {
            name: set(keys) for name, keys in (existing_keys or {}).items()
        }
        self.loaded: dict[Node, dict[str, object]] = {}
        # 尚未补写的暂缓外键：(节点, 外键名) -> 外键列
        self.open_deferrals: dict[tuple[Node, str], tuple[str, ...]] = {}

    # ------------------------------------------------------------------
    def apply_insert(self, stmt: Insert) -> None:
        """模拟执行一条 INSERT，违约束即抛 :class:`ConstraintViolationError`。"""
        spec = self._table(stmt.table)
        node = (stmt.table, stmt.key)
        values = stmt.values

        # 1) 主键：必须齐全、非空且不与已有数据冲突。
        for kc in spec.key_columns:
            if kc not in values or values[kc] is None:
                raise ConstraintViolationError(
                    f"INSERT 到 {stmt.table} 时主键列 {kc!r} 缺失或为 NULL",
                    stmt,
                )
        if stmt.key in self.existing.get(stmt.table, set()):
            raise ConstraintViolationError(
                f"主键 {stmt.table}{stmt.key} 已在库中存在", stmt
            )
        if node in self.loaded:
            raise ConstraintViolationError(
                f"主键 {stmt.table}{stmt.key} 在本批中重复插入", stmt
            )

        # 2) 非空列：首次插入必须满足（不能靠后续 UPDATE 补）。
        for col in spec.columns:
            if not col.nullable and values.get(col.name) is None:
                raise ConstraintViolationError(
                    f"非空列 {stmt.table}.{col.name} 在首次 INSERT 时为 NULL",
                    stmt,
                )

        deferred_names = {d.fk_name for d in stmt.deferred}
        for fk in spec.foreign_keys:
            self._check_fk_on_insert(spec, node, fk, values, deferred_names, stmt)

        # 3) 登记暂缓外键与已装载行。
        for d in stmt.deferred:
            self.open_deferrals[(node, d.fk_name)] = d.columns
        self.loaded[node] = dict(values)

    def _check_fk_on_insert(
        self,
        spec: TableSpec,
        node: Node,
        fk: ForeignKeySpec,
        values: dict[str, object],
        deferred_names: set[str],
        stmt: Insert,
    ) -> None:
        present = tuple(values.get(c) for c in fk.columns)
        is_null = [v is None for v in present]

        if any(is_null):
            if not all(is_null):
                raise ConstraintViolationError(
                    f"复合外键 {fk.name} 只置空了部分列，复合外键只能整组暂缓",
                    stmt,
                )
            # 整组 NULL：可能是规划器声明的暂缓，也可能是业务上本就为空。
            if fk.name in deferred_names:
                for col_name in fk.columns:
                    if not spec.column_map[col_name].nullable:
                        raise ConstraintViolationError(
                            f"外键 {fk.name} 含非空列 {col_name!r}，不能暂缓",
                            stmt,
                        )
            # 未声明暂缓的整组 NULL 视为天然空值，非空列已在前面的
            # NOT NULL 检查中拦截，这里直接放行。
            return

        target_key = present
        self._assert_target_exists(fk, target_key, stmt)

    def apply_update(self, stmt: Update) -> None:
        """模拟执行一条补写 UPDATE。"""
        spec = self._table(stmt.table)
        node = (stmt.table, stmt.key)
        if node not in self.loaded:
            raise ConstraintViolationError(
                f"UPDATE 目标行 {stmt.table}{stmt.key} 尚未插入", stmt
            )
        fk = spec.fk_map.get(stmt.fk_name)
        if fk is None:
            raise ConstraintViolationError(
                f"表 {stmt.table} 没有外键 {stmt.fk_name!r}", stmt
            )
        if set(stmt.assignments) != set(fk.columns):
            raise ConstraintViolationError(
                f"补写 {fk.name} 必须整组赋值 {fk.columns}", stmt
            )
        marker = (node, stmt.fk_name)
        if marker not in self.open_deferrals:
            raise ConstraintViolationError(
                f"外键 {stmt.fk_name} 没有处于暂缓状态，不能补写", stmt
            )
        new_value = tuple(stmt.assignments[c] for c in fk.columns)
        if any(v is None for v in new_value):
            raise ConstraintViolationError(
                "补写语句不能写入 NULL，必须给出真实外键值", stmt
            )
        self._assert_target_exists(fk, new_value, stmt)

        self.loaded[node].update(stmt.assignments)
        del self.open_deferrals[marker]

    def assert_all_closed(self) -> None:
        """所有暂缓外键都必须已补写。"""
        if self.open_deferrals:
            details = ", ".join(
                f"{n[0]}{n[1]}.{name}" for (n, name) in self.open_deferrals
            )
            raise ConstraintViolationError(f"仍有外键未补写：{details}")

    # ------------------------------------------------------------------
    def _assert_target_exists(
        self, fk: ForeignKeySpec, target_key: tuple, stmt: object
    ) -> None:
        if target_key in self.existing.get(fk.referenced_table, set()):
            return
        if (fk.referenced_table, target_key) in self.loaded:
            return
        raise ConstraintViolationError(
            f"外键 {fk.name} 引用的 {fk.referenced_table}{target_key} 尚不存在",
            stmt,
        )

    def _table(self, name: str) -> TableSpec:
        spec = self.tables.get(name)
        if spec is None:
            raise ConstraintViolationError(f"未知的表 {name!r}")
        return spec
