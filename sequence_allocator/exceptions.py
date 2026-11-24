"""序列分配器的异常体系。"""

from __future__ import annotations


class SequenceError(Exception):
    """所有序列分配器异常的基类。"""


class SequenceConfigError(SequenceError):
    """配置非法或管理变更参数不合法时抛出。"""


class SequenceNotFoundError(SequenceError):
    """目录中找不到指定名称的序列时抛出。"""


class SequenceExhaustedError(SequenceError):
    """非绕回序列到达界限、已无号码可发时抛出。"""


class SequenceSessionClosedError(SequenceError):
    """在已关闭（或已被收回失效）的会话上继续取号时抛出。"""
