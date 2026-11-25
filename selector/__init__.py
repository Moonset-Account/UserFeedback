"""可重放的服务网格端点选择器。

公开 API：

- :class:`EndpointSelector`：主选择器，接收权重/健康/请求事件与随机字节流。
- :class:`Lease`：一次 ``start_request`` 返回的租约，归还时传回 ``end_request``。
- :class:`RandomByteStream`：调用方提供的可重放随机字节流（bytes / 文件对象 / 迭代器）。
- 异常类型：:class:`NoEndpointAvailableError`、:class:`UnknownEndpointError`、
  :class:`InvalidWeightError`、:class:`InvalidLeaseError`、
  :class:`RandomnessExhaustedError`。
"""

from .errors import (
    DuplicateLeaseError,
    EndpointSelectorError,
    InvalidLeaseError,
    InvalidWeightError,
    NoEndpointAvailableError,
    RandomnessExhaustedError,
    UnknownEndpointError,
)
from .random_source import RandomByteStream
from .selector import EndpointSelector, Lease

__all__ = [
    "EndpointSelector",
    "Lease",
    "RandomByteStream",
    "EndpointSelectorError",
    "NoEndpointAvailableError",
    "UnknownEndpointError",
    "InvalidWeightError",
    "InvalidLeaseError",
    "DuplicateLeaseError",
    "RandomnessExhaustedError",
]
