"""离线授权审计库.

核查并发撤权/变更期间,授权服务是否错误返回了受保护数据.

核心规则:一次读取在调用时段内看到的字段集合,必须能由该时段内
*某一个时刻* 的完整授权状态解释;不允许把两代授权各自允许的字段
拼接到一起来解释一次返回。
"""

from .models import (
    AuditInput,
    FieldGrading,
    PermissionChange,
    ReadRecord,
    Subject,
    expand_level_grant,
)
from .checker import Auditor, AuditResult, Verdict

__all__ = [
    "AuditInput",
    "FieldGrading",
    "PermissionChange",
    "ReadRecord",
    "Subject",
    "expand_level_grant",
    "Auditor",
    "AuditResult",
    "Verdict",
]
