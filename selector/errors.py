"""选择器相关的异常类型。"""

from __future__ import annotations


class EndpointSelectorError(Exception):
    """所有选择器错误的基类。"""


class NoEndpointAvailableError(EndpointSelectorError):
    """没有任何健康端点可承接请求（空池）。"""


class UnknownEndpointError(EndpointSelectorError):
    """操作引用了池中不存在的端点编号。"""


class InvalidWeightError(EndpointSelectorError):
    """端点权重非法（必须为正整数）。"""


class InvalidLeaseError(EndpointSelectorError):
    """归还请求时传入了无法识别的租约。"""


class DuplicateLeaseError(InvalidLeaseError):
    """同一个租约被重复归还。"""


class RandomnessExhaustedError(EndpointSelectorError):
    """随机字节流已耗尽，无法继续抽样。"""
