"""RPA 执行过程中的显式错误类型。

所有错误都带稳定错误码，网页与证据中按错误码展示具体失败原因。
"""
from __future__ import annotations


class RpaError(Exception):
    """所有 RPA 错误的基类。"""

    code = "RPA_ERROR"

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class ProtocolError(RpaError):
    """任务文件不符合协议子集。"""

    code = "PROTOCOL_VIOLATION"


class AmbiguousCommandError(RpaError):
    """按完整菜单路径仍无法唯一确定命令。"""

    code = "AMBIGUOUS_COMMAND"


class DisabledCommandError(RpaError):
    """目标命令在当前上下文处于禁用状态，执行器必须停止。"""

    code = "COMMAND_DISABLED"


class CommandNotFoundError(RpaError):
    """完整菜单路径在当前菜单树中不存在。"""

    code = "COMMAND_NOT_FOUND"


class StateVerificationError(RpaError):
    """操作完成后，可见状态与预期不一致。"""

    code = "STATE_VERIFICATION_FAILED"


class BrowserInteractionError(RpaError):
    """浏览器自动化交互失败（元素未出现、点击超时等）。"""

    code = "BROWSER_INTERACTION_FAILED"


class CollectorBoundaryError(RpaError):
    """采集器试图访问协议允许范围之外的资源。"""

    code = "COLLECTOR_BOUNDARY_VIOLATION"
