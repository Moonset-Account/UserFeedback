"""删除语句的错误类型与结构化阻塞记录。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple


@dataclass(frozen=True)
class BlockedReference:
    """一条幸存引用：引用行没有被本语句删除，却指向了一个被删除目标。

    foreign_key       触发阻塞的外键约束名
    referencing_table 引用方表名
    referencing_key   引用方主键元组
    referencing_row   引用方完整行数据（快照副本）
    referenced_table  被引用表名
    referenced_key    被删除目标的主键元组
    referenced_row    被删除目标的完整行数据（快照副本）
    """

    foreign_key: str
    referencing_table: str
    referencing_key: Tuple[Any, ...]
    referencing_row: Dict[str, Any]
    referenced_table: str
    referenced_key: Tuple[Any, ...]
    referenced_row: Dict[str, Any]

    def describe(self) -> str:
        return (
            f"foreign key {self.foreign_key!r}: surviving row "
            f"{self.referencing_table}{self.referencing_key} still references "
            f"deleted row {self.referenced_table}{self.referenced_key}"
        )


class DeleteBlockedError(Exception):
    """语句末检查发现幸存引用时抛出；携带全部阻塞明细。

    抛出发生在任何物理删除之前，因此捕获该异常即意味着整条语句已回退、
    库内数据与语句开始前完全一致。
    """

    def __init__(self, violations: list[BlockedReference]) -> None:
        if not violations:
            raise ValueError("DeleteBlockedError requires at least one violation")
        self.violations: list[BlockedReference] = list(violations)
        head = "; ".join(v.describe() for v in violations[:3])
        more = "" if len(violations) <= 3 else f" (+{len(violations) - 3} more)"
        super().__init__(
            f"DELETE blocked by {len(violations)} surviving reference(s): {head}{more}"
        )
