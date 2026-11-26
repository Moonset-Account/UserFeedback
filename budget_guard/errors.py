"""图结构校验错误。

当操作图本身不符合受限模式（缺少必填字段、op 未知、字段类型错误等）时
抛出。这类错误意味着元数据不可信，导入端应直接拒绝整张图；分析阶段
"缺少外部证据" 不抛异常，而是生成带操作位置的 blocking Issue。
"""

from __future__ import annotations

from typing import Any, Optional


class GraphValidationError(ValueError):
    """操作图不是合法的受限操作图。"""

    def __init__(self, message: str, *, pos: Optional[str] = None, path: Optional[str] = None):
        full = message
        if pos and pos not in message:
            full = f"{message} (操作位置 pos={pos!r})"
        elif path and path not in message:
            full = f"{message} (路径 path={path!r})"
        super().__init__(full)
        self.message = message
        self.pos = pos
        self.path = path
