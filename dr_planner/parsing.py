"""字典 / JSON 风格数据解析助手。

支持的结构示例::

    {
        "service_id": "orders",
        "weight": 3,
        "backups": [
            {
                "backup_id": "b1",
                "position": 100,
                "bytes_required": 2048,
                "requirements": {"inventory": 80}
            }
        ]
    }
"""

from __future__ import annotations

from typing import Any, Mapping

from .model import Backup, Service


def backup_from_dict(data: Mapping[str, Any]) -> Backup:
    """从映射构造 :class:`Backup`。"""

    if not isinstance(data, Mapping):
        raise ValueError("备份配置必须是映射对象")
    try:
        requirements = data.get("requirements", {}) or {}
        if not isinstance(requirements, Mapping):
            raise ValueError("requirements 必须是服务标识到最低位置的映射")
        return Backup(
            backup_id=data["backup_id"],
            position=data["position"],
            bytes_required=data["bytes_required"],
            requirements=frozenset(requirements.items()),
        )
    except KeyError as exc:
        raise ValueError(f"备份配置缺少字段: {exc.args[0]}") from exc


def service_from_dict(data: Mapping[str, Any]) -> Service:
    """从映射构造 :class:`Service`。"""

    if not isinstance(data, Mapping):
        raise ValueError("服务配置必须是映射对象")
    try:
        raw_backups = data["backups"]
    except KeyError as exc:
        raise ValueError(f"服务配置缺少字段: {exc.args[0]}") from exc
    if not isinstance(raw_backups, (list, tuple)):
        raise ValueError("backups 必须是列表")
    return Service(
        service_id=data["service_id"],
        weight=data.get("weight", 1),
        backups=tuple(backup_from_dict(item) for item in raw_backups),
    )
