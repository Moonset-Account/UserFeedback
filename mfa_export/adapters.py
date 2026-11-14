"""本地测试适配器：可控时钟、假 SSO、假 MFA。

这些适配器模拟原需求中的外部系统：
- SSO 适配器：提供已验证的租户、用户及会话标识，以及导出权限判定。
- MFA 适配器：提供指定挑战的验证结果。

调用方（测试 / 演示）可以通过这些适配器控制时钟与身份状态，
从而重现挑战超时、会话切换、权限撤回、许可已使用等情况。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class Identity:
    """SSO 适配器验证通过后返回的身份。"""

    tenant_id: str
    user_id: str
    session_id: str


class ManualClock:
    """可手动推进的时钟，供测试控制时间。"""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        self._now += float(seconds)
        return self._now

    def set(self, value: float) -> None:
        self._now = float(value)


class SystemClock:
    """真实时钟（生产路径；本地交付中仅用于演示对照）。"""

    def now(self) -> float:
        return time.time()


class FakeSSOAdapter:
    """假 SSO 适配器。

    - register_identity: 登记 token -> 已验证身份（租户/用户/会话）。
    - set_export_permission: 控制某租户+用户+范围的导出权限，可随时撤回。
    """

    def __init__(self) -> None:
        self._tokens: Dict[str, Identity] = {}
        self._permissions: Dict[Tuple[str, str, str], bool] = {}

    def register_identity(
        self, token: str, tenant_id: str, user_id: str, session_id: str
    ) -> None:
        self._tokens[token] = Identity(
            tenant_id=tenant_id, user_id=user_id, session_id=session_id
        )

    def set_export_permission(
        self, tenant_id: str, user_id: str, scope: str, allowed: bool
    ) -> None:
        self._permissions[(tenant_id, user_id, scope)] = bool(allowed)

    # ---- 服务侧调用的接口 ----

    def verify_token(self, token: str) -> Optional[Identity]:
        """验证 SSO token，返回已验证身份；无效返回 None。"""
        return self._tokens.get(token)

    def has_export_permission(self, identity: Identity, scope: str) -> bool:
        """判定该身份当前是否持有指定范围的导出权限。"""
        return self._permissions.get(
            (identity.tenant_id, identity.user_id, scope), False
        )


class FakeMFAAdapter:
    """假 MFA 适配器。

    期望值（敏感验证凭证）只存在于服务端适配器内部，
    服务不会把它写入审计或返回给调用方。
    """

    def __init__(self, default_proof: Optional[str] = "correct-proof") -> None:
        self._expected: Dict[str, Optional[str]] = {}
        self._default_proof = default_proof

    def set_expected_proof(self, challenge_id: str, proof: Optional[str]) -> None:
        """为指定挑战设置期望凭证；None 表示该挑战永远无法通过。"""
        self._expected[challenge_id] = proof

    # ---- 服务侧调用的接口 ----

    def verify(self, challenge_id: str, proof: Optional[str]) -> bool:
        """返回指定挑战的验证结果。"""
        expected = self._expected.get(challenge_id, self._default_proof)
        if expected is None or proof is None:
            return False
        return proof == expected
