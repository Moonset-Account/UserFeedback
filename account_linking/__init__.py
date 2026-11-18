"""账户关联模块：机构合并场景下的外部身份账户归并。

公开入口：
    AccountLinkingService  —— 核心服务
    OwnershipProof         —— 经过验证且带有效期的所有权证明
    Account / ExternalIdentity —— 领域模型
    errors                 —— 业务异常
"""

from .errors import (
    AccountLinkingError,
    IdentityAlreadyLinkedError,
    InvalidKeepAccountError,
    ProofExpiredError,
    ProofInvalidError,
    SessionFinalizedError,
    SessionNotFoundError,
    SessionNotReadyError,
)
from .models import Account, ExternalIdentity, LinkRecord, LinkSession, SessionStatus
from .service import AccountLinkingService, OwnershipProof

__all__ = [
    "Account",
    "AccountLinkingError",
    "AccountLinkingService",
    "ExternalIdentity",
    "IdentityAlreadyLinkedError",
    "InvalidKeepAccountError",
    "LinkRecord",
    "LinkSession",
    "OwnershipProof",
    "ProofExpiredError",
    "ProofInvalidError",
    "SessionFinalizedError",
    "SessionNotFoundError",
    "SessionNotReadyError",
    "SessionStatus",
]
