"""核心服务：发起 MFA、确认挑战、兑换许可。

语义（与原需求逐条对应）：
- 发起 MFA：SSO 适配器验证 token 得到租户/用户/会话，挑战绑定
  该租户、用户、会话与导出范围；挑战 120 秒内可确认。
- 确认挑战：MFA 适配器给出指定挑战的验证结果；每个挑战最多签发
  一张许可；许可自签发起 300 秒内可兑换。
- 兑换许可：重新核对当前会话与导出权限；同一许可的并发兑换在锁内
  完成"检查-置位"，只有一个请求能拿到租户的密钥访问审计记录。

时间语义：now < expires_at 时有效，到达 expires_at 即视为过期。
敏感验证凭证（SSO token、MFA proof）不写入审计、不出现在返回值中。
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass
from typing import Dict, Optional

from .audit import AuditLog

CHALLENGE_TTL_SECONDS = 120  # 挑战两分钟内可确认
LICENSE_TTL_SECONDS = 300  # 许可签发后五分钟内可兑换


class Reason:
    """审计原因码（判定结果及原因）。"""

    OK_CHALLENGE_ISSUED = "OK_CHALLENGE_ISSUED"
    OK_LICENSE_ISSUED = "OK_LICENSE_ISSUED"
    OK_LICENSE_REDEEMED = "OK_LICENSE_REDEEMED"

    ERR_SSO_INVALID = "ERR_SSO_INVALID"
    ERR_EXPORT_PERMISSION_DENIED = "ERR_EXPORT_PERMISSION_DENIED"
    ERR_CHALLENGE_NOT_FOUND = "ERR_CHALLENGE_NOT_FOUND"
    ERR_CHALLENGE_EXPIRED = "ERR_CHALLENGE_EXPIRED"  # 挑战超时
    ERR_CHALLENGE_ALREADY_CONSUMED = "ERR_CHALLENGE_ALREADY_CONSUMED"  # 一挑战一许可
    ERR_MFA_VERIFICATION_FAILED = "ERR_MFA_VERIFICATION_FAILED"
    ERR_LICENSE_NOT_FOUND = "ERR_LICENSE_NOT_FOUND"
    ERR_LICENSE_EXPIRED = "ERR_LICENSE_EXPIRED"  # 许可过期
    ERR_LICENSE_ALREADY_USED = "ERR_LICENSE_ALREADY_USED"  # 许可已使用
    ERR_IDENTITY_MISMATCH = "ERR_IDENTITY_MISMATCH"
    ERR_SESSION_MISMATCH = "ERR_SESSION_MISMATCH"  # 会话切换
    ERR_PERMISSION_REVOKED = "ERR_PERMISSION_REVOKED"  # 权限撤回


@dataclass
class Challenge:
    challenge_id: str
    tenant_id: str
    user_id: str
    session_id: str
    scope: str
    created_at: float
    expires_at: float
    license_id: Optional[str] = None  # 每个挑战最多签发一张许可


@dataclass
class License:
    license_id: str
    challenge_id: str
    tenant_id: str
    user_id: str
    session_id: str
    scope: str
    issued_at: float
    expires_at: float
    redeemed: bool = False
    redeemed_at: Optional[float] = None


class ExportLicenseService:
    """三个接口：initiate_mfa / confirm_challenge / redeem_license。"""

    def __init__(self, sso_adapter, mfa_adapter, clock, audit_log: Optional[AuditLog] = None):
        self._sso = sso_adapter
        self._mfa = mfa_adapter
        self._clock = clock
        self._audit = audit_log if audit_log is not None else AuditLog()
        # 单把可重入锁保护全部状态；兑换的"检查-置位"在锁内完成，
        # 保证同一许可并发兑换时只有一个请求成功。
        self._lock = threading.RLock()
        self._challenges: Dict[str, Challenge] = {}
        self._licenses: Dict[str, License] = {}
        self._challenge_seq = itertools.count(1)
        self._license_seq = itertools.count(1)

    @property
    def audit(self) -> AuditLog:
        return self._audit

    # ------------------------------------------------------------------
    # 接口一：发起 MFA
    # ------------------------------------------------------------------
    def initiate_mfa(self, sso_token: str, export_scope: str) -> dict:
        """SSO 验证通过后，发起绑定租户/用户/会话/导出范围的挑战。"""
        with self._lock:
            now = self._clock.now()
            identity = self._sso.verify_token(sso_token)
            if identity is None:
                self._audit.append(
                    ts=now, event="initiate_mfa", decision="deny",
                    reason=Reason.ERR_SSO_INVALID, scope=export_scope,
                )
                return {"ok": False, "reason": Reason.ERR_SSO_INVALID}

            if not self._sso.has_export_permission(identity, export_scope):
                self._audit.append(
                    ts=now, event="initiate_mfa", decision="deny",
                    reason=Reason.ERR_EXPORT_PERMISSION_DENIED,
                    tenant_id=identity.tenant_id, user_id=identity.user_id,
                    session_id=identity.session_id, scope=export_scope,
                )
                return {"ok": False, "reason": Reason.ERR_EXPORT_PERMISSION_DENIED}

            challenge_id = "ch-%04d" % next(self._challenge_seq)
            challenge = Challenge(
                challenge_id=challenge_id,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                session_id=identity.session_id,
                scope=export_scope,
                created_at=now,
                expires_at=now + CHALLENGE_TTL_SECONDS,
            )
            self._challenges[challenge_id] = challenge
            self._audit.append(
                ts=now, event="initiate_mfa", decision="allow",
                reason=Reason.OK_CHALLENGE_ISSUED,
                tenant_id=identity.tenant_id, user_id=identity.user_id,
                session_id=identity.session_id, challenge_id=challenge_id,
                scope=export_scope,
            )
            return {
                "ok": True,
                "challenge_id": challenge_id,
                "tenant_id": identity.tenant_id,
                "user_id": identity.user_id,
                "session_id": identity.session_id,
                "scope": export_scope,
                "expires_at": challenge.expires_at,
            }

    # ------------------------------------------------------------------
    # 接口二：确认挑战（每个挑战最多签发一张许可）
    # ------------------------------------------------------------------
    def confirm_challenge(self, challenge_id: str, mfa_proof: Optional[str]) -> dict:
        """MFA 适配器验证指定挑战；通过后签发一张五分钟内可兑换的许可。"""
        with self._lock:
            now = self._clock.now()
            challenge = self._challenges.get(challenge_id)
            if challenge is None:
                self._audit.append(
                    ts=now, event="confirm_challenge", decision="deny",
                    reason=Reason.ERR_CHALLENGE_NOT_FOUND, challenge_id=challenge_id,
                )
                return {"ok": False, "reason": Reason.ERR_CHALLENGE_NOT_FOUND}

            base = dict(
                tenant_id=challenge.tenant_id, user_id=challenge.user_id,
                session_id=challenge.session_id, challenge_id=challenge_id,
                scope=challenge.scope,
            )

            if now >= challenge.expires_at:
                self._audit.append(
                    ts=now, event="confirm_challenge", decision="deny",
                    reason=Reason.ERR_CHALLENGE_EXPIRED, **base,
                )
                return {"ok": False, "reason": Reason.ERR_CHALLENGE_EXPIRED}

            if challenge.license_id is not None:
                self._audit.append(
                    ts=now, event="confirm_challenge", decision="deny",
                    reason=Reason.ERR_CHALLENGE_ALREADY_CONSUMED, **base,
                )
                return {"ok": False, "reason": Reason.ERR_CHALLENGE_ALREADY_CONSUMED}

            # 敏感凭证只传给服务端 MFA 适配器，不写入审计、不进入返回值。
            if not self._mfa.verify(challenge_id, mfa_proof):
                self._audit.append(
                    ts=now, event="confirm_challenge", decision="deny",
                    reason=Reason.ERR_MFA_VERIFICATION_FAILED, **base,
                )
                return {"ok": False, "reason": Reason.ERR_MFA_VERIFICATION_FAILED}

            license_id = "lic-%04d" % next(self._license_seq)
            lic = License(
                license_id=license_id,
                challenge_id=challenge_id,
                tenant_id=challenge.tenant_id,
                user_id=challenge.user_id,
                session_id=challenge.session_id,
                scope=challenge.scope,
                issued_at=now,
                expires_at=now + LICENSE_TTL_SECONDS,
            )
            self._licenses[license_id] = lic
            challenge.license_id = license_id
            self._audit.append(
                ts=now, event="confirm_challenge", decision="allow",
                reason=Reason.OK_LICENSE_ISSUED, license_id=license_id, **base,
            )
            return {
                "ok": True,
                "license_id": license_id,
                "challenge_id": challenge_id,
                "expires_at": lic.expires_at,
            }

    # ------------------------------------------------------------------
    # 接口三：兑换许可（原子；重新核对会话与导出权限）
    # ------------------------------------------------------------------
    def redeem_license(self, license_id: str, sso_token: str) -> dict:
        """兑换许可。同一许可并发兑换时只有一个请求拿到导出载荷。"""
        with self._lock:
            now = self._clock.now()
            identity = self._sso.verify_token(sso_token)
            if identity is None:
                self._audit.append(
                    ts=now, event="redeem_license", decision="deny",
                    reason=Reason.ERR_SSO_INVALID, license_id=license_id,
                )
                return {"ok": False, "reason": Reason.ERR_SSO_INVALID}

            lic = self._licenses.get(license_id)
            if lic is None:
                self._audit.append(
                    ts=now, event="redeem_license", decision="deny",
                    reason=Reason.ERR_LICENSE_NOT_FOUND,
                    tenant_id=identity.tenant_id, user_id=identity.user_id,
                    session_id=identity.session_id, license_id=license_id,
                )
                return {"ok": False, "reason": Reason.ERR_LICENSE_NOT_FOUND}

            base = dict(
                tenant_id=identity.tenant_id, user_id=identity.user_id,
                session_id=identity.session_id, challenge_id=lic.challenge_id,
                license_id=license_id, scope=lic.scope,
            )

            # 许可已使用（含并发场景下输掉竞争的请求）
            if lic.redeemed:
                self._audit.append(
                    ts=now, event="redeem_license", decision="deny",
                    reason=Reason.ERR_LICENSE_ALREADY_USED, **base,
                )
                return {"ok": False, "reason": Reason.ERR_LICENSE_ALREADY_USED}

            if now >= lic.expires_at:
                self._audit.append(
                    ts=now, event="redeem_license", decision="deny",
                    reason=Reason.ERR_LICENSE_EXPIRED, **base,
                )
                return {"ok": False, "reason": Reason.ERR_LICENSE_EXPIRED}

            if identity.tenant_id != lic.tenant_id or identity.user_id != lic.user_id:
                self._audit.append(
                    ts=now, event="redeem_license", decision="deny",
                    reason=Reason.ERR_IDENTITY_MISMATCH, **base,
                )
                return {"ok": False, "reason": Reason.ERR_IDENTITY_MISMATCH}

            # 会话切换：同一用户但当前会话与签发时绑定的会话不同
            if identity.session_id != lic.session_id:
                self._audit.append(
                    ts=now, event="redeem_license", decision="deny",
                    reason=Reason.ERR_SESSION_MISMATCH, **base,
                )
                return {"ok": False, "reason": Reason.ERR_SESSION_MISMATCH}

            # 权限撤回：兑换时重新核对当前导出权限
            if not self._sso.has_export_permission(identity, lic.scope):
                self._audit.append(
                    ts=now, event="redeem_license", decision="deny",
                    reason=Reason.ERR_PERMISSION_REVOKED, **base,
                )
                return {"ok": False, "reason": Reason.ERR_PERMISSION_REVOKED}

            # 检查-置位在锁内完成：并发下只有一个请求能走到这里
            lic.redeemed = True
            lic.redeemed_at = now
            self._audit.append(
                ts=now, event="redeem_license", decision="allow",
                reason=Reason.OK_LICENSE_REDEEMED, **base,
            )
            return {"ok": True, "license_id": license_id,
                    "payload": self._build_export_payload(lic, now)}

    # ------------------------------------------------------------------
    @staticmethod
    def _build_export_payload(lic: License, now: float) -> dict:
        """租户的密钥访问审计记录（本地合成的导出载荷）。"""
        return {
            "tenant_id": lic.tenant_id,
            "scope": lic.scope,
            "license_id": lic.license_id,
            "generated_at": now,
            "key_access_audit": [
                {
                    "key_id": "%s-master-key" % lic.tenant_id,
                    "operation": "unwrap",
                    "actor": "export-service",
                    "at": now,
                }
            ],
        }
