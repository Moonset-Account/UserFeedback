"""业务异常定义。"""


class AccountLinkingError(Exception):
    """账户关联模块的基类异常。"""


class ProofInvalidError(AccountLinkingError):
    """所有权证明未通过验证（例如签名/校验位不合法）。"""


class ProofExpiredError(AccountLinkingError):
    """所有权证明已超出有效期。"""


class IdentityAlreadyLinkedError(AccountLinkingError):
    """外部身份已关联到其他保留账户，本次操作必须被阻止。"""


class InvalidKeepAccountError(AccountLinkingError):
    """指定的保留账户不属于本次关联的双方账户。"""


class SessionNotFoundError(AccountLinkingError):
    """关联会话不存在。"""


class SessionNotReadyError(AccountLinkingError):
    """双方证明尚未齐备，无法确认合并。"""


class SessionFinalizedError(AccountLinkingError):
    """会话已有最终结果（已确认或已撤回），后续操作被拒绝。"""
