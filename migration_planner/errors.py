"""规划器使用的异常类型。"""

from __future__ import annotations


class PlanningError(Exception):
    """规划过程中所有错误的基类。"""


class SchemaError(PlanningError):
    """输入的表结构 / 外键声明不合法。"""


class DanglingReferenceError(PlanningError):
    """外键引用的目标行既不在本批记录中，也不在库中已有的目标键内。"""

    def __init__(self, source: tuple[str, tuple], fk_name: str,
                 target_table: str, target_key: tuple):
        self.source = source
        self.fk_name = fk_name
        self.target_table = target_table
        self.target_key = target_key
        super().__init__(
            f"行 {source[0]}{source[1]} 的外键 {fk_name!r} 引用了不存在的 "
            f"{target_table}{target_key}"
        )


class ConstraintViolationError(PlanningError):
    """模拟执行某条语句时违反了主键、非空或外键约束。"""

    def __init__(self, message: str, statement: object | None = None):
        self.statement = statement
        if statement is not None:
            message = f"{message}；出错语句：{statement}"
        super().__init__(message)
