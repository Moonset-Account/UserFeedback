"""登录验证与哈希参数升级组件。

对外导出的主要入口：

- :class:`~login.service.LoginService`：验证、升级、改口令、停用账户
- :class:`~login.storage.JsonFileCredentialStore`：本地 JSON 模拟存储
- :class:`~login.storage.CredentialRecord`：凭据记录（含算法与参数版本）
- :class:`~login.service.Session`：成功验证后建立的会话
- :class:`~login.hashing` 下的参数集与当前参数版本
"""

from .exceptions import (
    AccountDisabled,
    ConcurrentModification,
    CredentialModifiedDuringLogin,
    InvalidPassword,
    LoginError,
    UpgradeInterrupted,
    UserNotFound,
)
from .hashing import ALGORITHM, CURRENT_PARAM_VERSION, PARAM_SETS, hash_password, verify_password
from .service import LoginService, LoginTracer, Session
from .storage import CredentialRecord, CredentialStore, JsonFileCredentialStore

__all__ = [
    "ALGORITHM",
    "CURRENT_PARAM_VERSION",
    "PARAM_SETS",
    "AccountDisabled",
    "ConcurrentModification",
    "CredentialModifiedDuringLogin",
    "CredentialRecord",
    "CredentialStore",
    "InvalidPassword",
    "JsonFileCredentialStore",
    "LoginError",
    "LoginService",
    "LoginTracer",
    "Session",
    "UpgradeInterrupted",
    "UserNotFound",
    "hash_password",
    "verify_password",
]
