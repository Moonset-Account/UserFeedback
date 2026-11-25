"""匿名通话中继号分配与回拨解析模块。

公开 API：
    - 数据模型: Reservation, Allocation, CallResult, CallStatus
    - 异常: AllocationError, InvalidReservationError, ConflictingPinError,
            InsufficientRelaysError, AmbiguousRoutingError
    - RelayAllocator: 预约校验、冲突判定、最少中继号分配（已公布预约固定原号）
    - resolve_callback: 回拨解析（唯一接通 / 已结束 / 拒绝陌生主叫）
    - conflicts: 两个预约是否冲突
    - 演示入口在子模块 ``anon_relay.demo``（build_demo / run_demo），
      刻意不在包初始化时导入，以便 ``python -m anon_relay.demo`` 直接运行。
"""

from .models import (
    Allocation,
    CallResult,
    CallStatus,
    Reservation,
)
from .allocator import (
    AllocationError,
    ConflictingPinError,
    InsufficientRelaysError,
    InvalidReservationError,
    RelayAllocator,
    conflicts,
)
from .callback import AmbiguousRoutingError, resolve_callback

__all__ = [
    "Reservation",
    "Allocation",
    "CallResult",
    "CallStatus",
    "AllocationError",
    "InvalidReservationError",
    "ConflictingPinError",
    "InsufficientRelaysError",
    "AmbiguousRoutingError",
    "RelayAllocator",
    "conflicts",
    "resolve_callback",
]
