"""存储行的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Tuple


@dataclass(frozen=True)
class Row:
    """一条父记录或子记录。

    :param row_id: 存储分配的全局单调行 id
    :param key: 父键 / 子记录指向的父键
    :param start: 半开有效期起点（含）
    :param end: 半开有效期终点（不含）
    """

    row_id: int
    key: Any
    start: Any
    end: Any

    @property
    def interval(self) -> Tuple[Any, Any]:
        return (self.start, self.end)
