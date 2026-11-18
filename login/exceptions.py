"""登录模块的异常类型。"""


class LoginError(Exception):
    """所有登录模块错误的基类。"""


class UserNotFound(LoginError):
    """本地存储中不存在该用户的凭据记录。"""


class InvalidPassword(LoginError):
    """口令验证失败。"""


class AccountDisabled(LoginError):
    """账户已被停用，必须稳定拒绝建立会话。"""


class CredentialModifiedDuringLogin(LoginError):
    """验证期间凭据记录被修改（例如用户改了口令），本次登录不得建立会话。"""


class UpgradeInterrupted(LoginError):
    """旧哈希升级过程中断；磁盘上的原记录保持不变，可用有效记录重试。"""


class ConcurrentModification(LoginError):
    """乐观锁冲突：记录版本与期望版本不一致，compare-and-swap 被拒绝。"""
