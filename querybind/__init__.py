"""querybind: 小型查询树的名称绑定、相关子查询改写与行集执行。

公开入口：
    bind(query, catalog)            -> 绑定后的查询树（含 BindReport）
    rewrite_query(query, catalog)   -> 绑定 + 相关标量子查询左连接改写（重新绑定）
    Executor(catalog).execute(tree) -> QueryResult
    Catalog / TableData             -> 本地合成元数据与行集
"""

from .errors import (
    AmbiguousColumnError,
    BinderError,
    DuplicateAliasError,
    IllegalLateralError,
    UnresolvedColumnError,
)
from .catalog import Catalog, TableData
from .model import (
    ColumnRef,
    DerivedTable,
    FuncCall,
    Join,
    Literal,
    SelectNode,
    Star,
    SubqueryExpr,
    TableRef,
    and_,
    col,
    eq,
    lit,
    not_,
    or_,
    star,
    subq,
)
from .binder import BindReport, FreeVar, OutputDescriptor, bind
from .rewrite import RewriteOutcome, rewrite_query
from .executor import Executor, QueryResult, ResultColumn

__all__ = [
    "bind",
    "rewrite_query",
    "Executor",
    "QueryResult",
    "ResultColumn",
    "Catalog",
    "TableData",
    "BindReport",
    "FreeVar",
    "OutputDescriptor",
    "BinderError",
    "AmbiguousColumnError",
    "UnresolvedColumnError",
    "IllegalLateralError",
    "DuplicateAliasError",
    "RewriteOutcome",
    "ColumnRef",
    "DerivedTable",
    "FuncCall",
    "Join",
    "Literal",
    "SelectNode",
    "Star",
    "SubqueryExpr",
    "TableRef",
    "and_",
    "col",
    "eq",
    "lit",
    "not_",
    "or_",
    "star",
    "subq",
]
