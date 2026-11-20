"""relengine：一个仅依赖标准库的内存关系引擎，实现可批量指定主键的删除语句。"""

from .database import Database
from .delete import DeletedRow, DeleteResult
from .errors import BlockedReference, DeleteBlockedError
from .schema import CASCADE, CHECK, ForeignKey, Table

__all__ = [
    "Database",
    "Table",
    "ForeignKey",
    "DeletedRow",
    "DeleteResult",
    "BlockedReference",
    "DeleteBlockedError",
    "CASCADE",
    "CHECK",
]
