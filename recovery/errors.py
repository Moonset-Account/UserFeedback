"""恢复会话模块的异常类型。

所有模块主动抛出的异常都继承 :class:`RecoveryError`，调用方可以按
``reason`` 字段做程序化分支，也可以直接展示面向用户的消息。
"""

from __future__ import annotations


class RecoveryError(Exception):
    """恢复流程相关异常的公共基类。"""


class RecoveryStateError(RecoveryError):
    """当前会话状态不允许该操作（例如对终态会话再次提交）。"""


class RecoveryRejected(RecoveryError):
    """提交恢复码/重新登记时被规则拒绝。

    :param reason: 机器可读的拒绝原因，取值见 :mod:`recovery.session`。
    :param message: 面向调用方的描述信息。
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class UnknownSessionError(RecoveryError):
    """按 ID 查找恢复会话时未找到。"""
