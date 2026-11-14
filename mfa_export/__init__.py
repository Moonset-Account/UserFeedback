"""MFA 挑战与一次性导出许可兑换（SEC-20261114-055）。

仅使用 Python 3 标准库。外部系统（SSO / MFA）以本地适配器模拟，
调用方可通过适配器控制时钟与身份状态。
"""

from .adapters import (
    Identity,
    ManualClock,
    SystemClock,
    FakeSSOAdapter,
    FakeMFAAdapter,
)
from .audit import AuditLog, AuditRecord
from .service import (
    ExportLicenseService,
    Reason,
    CHALLENGE_TTL_SECONDS,
    LICENSE_TTL_SECONDS,
)

__all__ = [
    "Identity",
    "ManualClock",
    "SystemClock",
    "FakeSSOAdapter",
    "FakeMFAAdapter",
    "AuditLog",
    "AuditRecord",
    "ExportLicenseService",
    "Reason",
    "CHALLENGE_TTL_SECONDS",
    "LICENSE_TTL_SECONDS",
]
