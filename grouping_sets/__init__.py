"""分批分组集合聚合算子。

公开 API::

    from grouping_sets import GroupingSetsSpec, GroupingSetsEngine

    spec = GroupingSetsSpec(
        grouping_columns=["region", "product"],
        grouping_sets=[["region", "product"], ["region"], []],
        aggregates=[("count", "*", "n"), ("sum", "amount", "amount_sum"),
                    ("min", "amount", "amount_min")],
    )
    engine = GroupingSetsEngine(spec)
    result = engine.apply(adds=[{"region": "north", "product": "a", "amount": 10}])
    result.upserts      # 本批真正改变的结果行
    result.deletes      # 本批消失的结果行
    result.snapshot     # 本批之后的完整快照

语义要点：

* 分组集合按调用方给定顺序排列，可重复；重复的集合各自独立维护状态，
  也各自输出一份结果。一次撤回对每个状态至多扣减一份。
* 每个结果行带 GROUPING 位图（见 ``model.ResultRow``），
  位为 1 的位置对应"被该分组集合省略的列"，其 ``None`` 是小计空值；
  位为 0 的 ``None`` 是数据本身的 NULL。两者靠位图而非哨兵值区分。
* COUNT 计非空值个数（``COUNT(*)`` 计行数），SUM/MIN 忽略空值；
  没有非空输入时 SUM/MIN 为 ``None``，空分组集合没有数据时
  COUNT 为 0、其余为 ``None``。
* 普通分组在最后一行被撤回后从结果中消失；空分组集合永远输出一行。
* 每批返回相对上一批的差量：``upserts`` 包含新增和内容真正变化的行，
  ``deletes`` 包含消失的行（携带消失前的值）。
"""

from .engine import GroupingSetsEngine, format_rows, rows_as_dicts
from .errors import (
    AggregateTypeError,
    DuplicateColumnError,
    GroupingSetsError,
    MissingColumnError,
    NonFiniteValueError,
    SchemaError,
    UnhashableGroupingValueError,
    UnknownColumnError,
    UnmatchedRetractionError,
)
from .model import (
    BatchResult,
    ResultRow,
    diff_snapshots,
    make_row,
    snapshot_map,
    sort_key_for,
)
from .schema import (
    AGGREGATE_FUNCS,
    COUNT_STAR,
    AggregateColumn,
    GroupingSet,
    GroupingSetsSpec,
)
from .values import is_null, normalize_value

__all__ = [
    "AGGREGATE_FUNCS",
    "COUNT_STAR",
    "AggregateColumn",
    "AggregateTypeError",
    "BatchResult",
    "DuplicateColumnError",
    "GroupingSet",
    "GroupingSetsEngine",
    "GroupingSetsError",
    "GroupingSetsSpec",
    "MissingColumnError",
    "NonFiniteValueError",
    "ResultRow",
    "SchemaError",
    "UnhashableGroupingValueError",
    "UnknownColumnError",
    "UnmatchedRetractionError",
    "diff_snapshots",
    "format_rows",
    "is_null",
    "make_row",
    "normalize_value",
    "rows_as_dicts",
    "snapshot_map",
    "sort_key_for",
]
