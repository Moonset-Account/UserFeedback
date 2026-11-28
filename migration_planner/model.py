"""规划器的数据模型（全部为不可变值对象）。

主键与外键的键值统一用 ``tuple`` 表示：单列键写 ``(value,)``，
复合键写 ``(v1, v2, ...)``。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ColumnSpec:
    """列定义。

    :param name: 列名。
    :param nullable: 是否允许 NULL。非空列（False）不能被临时置空，
        因此在首次 INSERT 时必须有值且所涉外键必须已满足。
    :param defer_cost: 该列允许被临时置空时，置空一次的代价；
        ``None`` 表示该列不允许临时置空。
    """

    name: str
    nullable: bool = True
    defer_cost: float | None = None


@dataclass(frozen=True)
class ForeignKeySpec:
    """外键定义（单列或复合）。

    :param name: 外键名称。
    :param columns: 本表里的外键列，复合外键给出多列，顺序与
        ``referenced_columns`` 一一对应。
    :param referenced_table: 被引用的目标表。
    :param referenced_columns: 目标表的被引用列（通常是主键列）。
    """

    name: str
    columns: tuple[str, ...]
    referenced_table: str
    referenced_columns: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.columns or len(self.columns) != len(self.referenced_columns):
            raise ValueError("外键列与被引用列必须等长且非空")


@dataclass(frozen=True)
class TableSpec:
    """一张目标表的结构。"""

    name: str
    key_columns: tuple[str, ...]
    columns: tuple[ColumnSpec, ...]
    foreign_keys: tuple[ForeignKeySpec, ...] = ()

    @property
    def column_map(self) -> dict[str, ColumnSpec]:
        return {c.name: c for c in self.columns}

    @property
    def fk_map(self) -> dict[str, ForeignKeySpec]:
        return {fk.name: fk for fk in self.foreign_keys}


@dataclass(frozen=True)
class Row:
    """一批待装载记录中的一行。

    :param table: 所属表名。
    :param values: 列名到列值的映射；``None`` 表示 SQL NULL。
    """

    table: str
    values: dict[str, object]

    def key(self, key_columns: tuple[str, ...]) -> tuple:
        missing = [c for c in key_columns if c not in self.values]
        if missing:
            raise ValueError(f"行 {self.table} 缺少主键列 {missing}")
        return tuple(self.values[c] for c in key_columns)


@dataclass(frozen=True)
class Insert:
    """一条 INSERT 语句。

    ``values`` 已包含被暂缓外键列的 NULL 占位；``deferred`` 记录本语句
    临时置空的外键（整组）。
    """

    table: str
    values: dict[str, object]
    key: tuple
    deferred: tuple[DeferredFK, ...] = ()

    def __str__(self) -> str:
        cols = ", ".join(self.values)
        vals = ", ".join("NULL" if v is None else repr(v)
                         for v in self.values.values())
        tail = f"  -- 暂缓外键: {', '.join(d.fk_name for d in self.deferred)}" \
            if self.deferred else ""
        return f"INSERT INTO {self.table} ({cols}) VALUES ({vals});{tail}"


@dataclass(frozen=True)
class Update:
    """一条补写（UPDATE）语句，把之前置空的外键列写回真实值。"""

    table: str
    key: tuple
    fk_name: str
    assignments: dict[str, object]

    def __str__(self) -> str:
        sets = ", ".join(f"{c} = {'NULL' if v is None else repr(v)}"
                         for c, v in self.assignments.items())
        key_cols = ", ".join(repr(k) for k in self.key)
        return f"UPDATE {self.table} SET {sets} WHERE key = ({key_cols});  -- {self.fk_name}"


@dataclass(frozen=True)
class DeferredFK:
    """一次暂缓决策：某行的某个外键（整组列）被临时置空及其代价。"""

    table: str
    row_key: tuple
    fk_name: str
    columns: tuple[str, ...]
    cost: float


@dataclass(frozen=True)
class CycleEdge:
    """不可打断依赖环上的一条边。"""

    source_table: str
    source_key: tuple
    fk_name: str
    target_table: str
    target_key: tuple

    def __str__(self) -> str:
        return (
            f"{self.source_table}{self.source_key} "
            f"--[{self.fk_name}]--> "
            f"{self.target_table}{self.target_key}"
        )


@dataclass(frozen=True)
class PlanResult:
    """规划结果。

    可解时 ``feasible`` 为 True，给出语句序列与暂缓集合；无解时为 False，
    ``cycle`` 给出一条不可打断的依赖环。
    """

    feasible: bool
    statements: tuple[Insert | Update, ...] = ()
    inserts: tuple[Insert, ...] = ()
    updates: tuple[Update, ...] = ()
    deferred: tuple[DeferredFK, ...] = ()
    total_cost: float = 0.0
    cycle: tuple[CycleEdge, ...] = ()
    reason: str = ""

    def describe_cycle(self) -> str:
        if not self.cycle:
            return ""
        chain = " -> ".join(
            f"{e.source_table}{e.source_key}[{e.fk_name}]" for e in self.cycle
        )
        head = self.cycle[0]
        return f"{chain} -> {head.source_table}{head.source_key}"

    def to_sql(self) -> str:
        return "\n".join(str(s) for s in self.statements)
