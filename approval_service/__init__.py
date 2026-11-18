"""审批权限判定模块。

模块职责：
- 分别记录目录修订的生效时间（effective_at）与接收时间（received_at）；
- 支持基于“判定时刻系统可见输入”的当前授权；
- 支持针对既往决定的复核：历史更正可以改写复核结论，但原始决定及其
  可见输入快照、输入指纹始终保留，不被篡改。
"""

from .model import (
    AccessRequest,
    BusinessScope,
    Decision,
    DirectoryRecord,
    Outcome,
    ResourceRule,
    ReviewOutcome,
    ReviewReport,
)
from .catalog import DirectoryCatalog, ViewResolution, resolve_view
from .service import ApprovalService

__all__ = [
    "AccessRequest",
    "ApprovalService",
    "BusinessScope",
    "Decision",
    "DirectoryCatalog",
    "DirectoryRecord",
    "Outcome",
    "ResourceRule",
    "ReviewOutcome",
    "ReviewReport",
    "ViewResolution",
    "resolve_view",
]
