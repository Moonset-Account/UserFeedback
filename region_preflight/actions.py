"""步骤动作注册表。

动作按「是否让数据在该区域落地（持久化）」分为两类：

* ``persists=True``：缓存写、异步日志落盘、容灾副本等，数据会留在该区域；
* ``persists=False``：缓存读、纯转发等，链路途经但不落盘，另行记录为转发节点。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionSpec:
    """单个步骤动作的语义说明。"""

    action: str
    persists: bool
    description: str = ""


# 内置动作。配置文件可以通过顶层 ``actions`` 字段扩展或覆盖。
DEFAULT_ACTIONS: dict[str, ActionSpec] = {
    # 缓存步骤
    "cache_read": ActionSpec("cache_read", False, "读取缓存，链路途经不在该区域落盘"),
    "cache_write": ActionSpec("cache_write", True, "写入缓存，数据在该区域落地"),
    # 异步日志步骤
    "async_log": ActionSpec("async_log", True, "异步日志落盘，数据在该区域持久化"),
    # 容灾副本步骤
    "replicate": ActionSpec("replicate", True, "写入容灾副本，数据在该区域持久化"),
    # 纯转发网关节点
    "forward": ActionSpec("forward", False, "仅转发，链路途经不落盘"),
}
