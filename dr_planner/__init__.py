"""灾备恢复点选择工具包。

对外公开：
    Backup / Service / RecoveryResult —— 数据模型；
    plan_recovery —— 恢复点选择主入口；
    service_from_dict / backup_from_dict —— 字典数据解析助手。
"""

from .model import Backup, RecoveryResult, Service
from .planner import (
    MAX_BACKUPS_PER_SERVICE,
    MAX_SERVICES,
    REASON_BUDGET,
    REASON_EMPTY_SERVICE,
    REASON_INFEASIBLE_DEPS,
    plan_recovery,
)
from .parsing import backup_from_dict, service_from_dict

__all__ = [
    "Backup",
    "Service",
    "RecoveryResult",
    "plan_recovery",
    "service_from_dict",
    "backup_from_dict",
    "REASON_BUDGET",
    "REASON_INFEASIBLE_DEPS",
    "REASON_EMPTY_SERVICE",
    "MAX_SERVICES",
    "MAX_BACKUPS_PER_SERVICE",
]
