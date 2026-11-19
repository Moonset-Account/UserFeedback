"""分析库的专用异常。

所有异常都只用于描述*分析过程本身*的失败（输入结构非法、递归、
未知名函数等）。被分析函数内部抛出的“权限拒绝 / 普通错误”属于
结构化树中的叶节点数据，不是这里的异常。
"""

from __future__ import annotations


class DenialFlowError(Exception):
    """denialflow 所有异常的公共基类。"""


class InvalidNodeError(DenialFlowError):
    """结构化函数树的节点构造或组合不合法。"""

    def __init__(self, message: str, location: str | None = None) -> None:
        if location is not None:
            message = f"[{location}] {message}"
        super().__init__(message)
        self.location = location


class UnknownFunctionError(DenialFlowError):
    """调用节点引用了函数森林中不存在的函数名。"""

    def __init__(self, name: str, location: str | None = None) -> None:
        prefix = f"[{location}] " if location is not None else ""
        super().__init__(f"{prefix}调用了未定义的函数: {name!r}")
        self.name = name
        self.location = location


class RecursiveCallError(DenialFlowError):
    """调用图存在递归（直接或间接），题目要求分析无递归函数树。"""

    def __init__(self, cycle: tuple[str, ...]) -> None:
        self.cycle = cycle
        chain = " -> ".join(cycle)
        super().__init__(f"检测到递归调用链: {chain}")
