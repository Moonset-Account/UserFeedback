"""主机管控端服务基线引擎。

对需要低权限运行的服务，基于平台默认值、服务定义、覆盖层（含合并/清除
语义）解析最终配置，并结合进程实测快照核对：

1. 启动身份（user/group）；
2. 写入范围（快照中实际写入路径必须落在配置允许的写入路径内）；
3. 权限继承（进程身份是否为被禁止的高权限身份）。

别名引用的服务按最终定义判定；不符合项区分来源（配置 / 运行现场）。
"""

from .engine import BaselineEngine, evaluate
from .models import (
    BaselineReport,
    Finding,
    ListOp,
    Overlay,
    PlatformDefaults,
    ProcessSnapshot,
    ServiceDef,
    ServiceReport,
)

__all__ = [
    "BaselineEngine",
    "evaluate",
    "BaselineReport",
    "Finding",
    "ListOp",
    "Overlay",
    "PlatformDefaults",
    "ProcessSnapshot",
    "ServiceDef",
    "ServiceReport",
]
