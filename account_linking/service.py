"""账户关联核心服务。

并发模型：所有改变会话终态与账户归属的操作（confirm / withdraw）
都在同一把锁内完成"检查-决策-落账"，保证确认与撤回同时到达时
只产生一种最终结果。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .errors import (
    IdentityAlreadyLinkedError,
    InvalidKeepAccountError,
    ProofExpiredError,
    ProofInvalidError,
    SessionFinalizedError,
    SessionNotFoundError,
    SessionNotReadyError,
)
from .models import Account, ExternalIdentity, LinkRecord, LinkSession, SessionStatus


@dataclass(frozen=True)
class OwnershipProof:
    """一方对其外部身份的所有权证明。

    verified  由上游验证器（签名/令牌校验）置位，本模块只信任该结论；
    expires_at 证明的有效期截止时刻，过期即拒绝。
    """

    identity_id: str
    verified: bool
    expires_at: datetime

    def validate(self, now: datetime) -> None:
        if not self.verified:
            raise ProofInvalidError(f"identity {self.identity_id!r} 的证明未通过验证")
        if now >= self.expires_at:
            raise ProofExpiredError(f"identity {self.identity_id!r} 的证明已过期")


class AccountLinkingService:
    """账户关联服务（内存实现，线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._accounts: Dict[str, Account] = {}
        self._identities: Dict[str, ExternalIdentity] = {}
        self._identity_account: Dict[str, str] = {}  # identity_id -> 当前所属账户
        self._sessions: Dict[str, LinkSession] = {}
        self._link_records: List[LinkRecord] = []

    # ------------------------------------------------------------------
    # 注册与候选提示
    # ------------------------------------------------------------------
    def register_account(self, account_id: str, email: str) -> Account:
        with self._lock:
            account = Account(account_id=account_id, email=email)
            self._accounts[account_id] = account
            return account

    def register_identity(
        self, identity_id: str, provider: str, subject: str, account_id: str
    ) -> ExternalIdentity:
        with self._lock:
            if account_id not in self._accounts:
                raise KeyError(f"账户不存在: {account_id}")
            identity = ExternalIdentity(
                identity_id=identity_id, provider=provider, subject=subject
            )
            self._identities[identity_id] = identity
            self._identity_account[identity_id] = account_id
            self._accounts[account_id].identity_ids.add(identity_id)
            return identity

    def suggest_candidates(self, email: str) -> List[Account]:
        """邮箱相同仅作为候选提示，绝不自动建立任何关联。"""
        with self._lock:
            return [a for a in self._accounts.values() if a.email == email]

    # ------------------------------------------------------------------
    # 关联会话
    # ------------------------------------------------------------------
    def start_link_session(
        self, account_a_id: str, account_b_id: str, keep_account_id: str
    ) -> LinkSession:
        """由用户指定保留账户，开启一次关联会话。"""
        with self._lock:
            if account_a_id == account_b_id:
                raise InvalidKeepAccountError("双方账户不能是同一个账户")
            for aid in (account_a_id, account_b_id):
                if aid not in self._accounts:
                    raise KeyError(f"账户不存在: {aid}")
                if self._accounts[aid].merged_into is not None:
                    raise IdentityAlreadyLinkedError(
                        f"账户 {aid} 已并入 {self._accounts[aid].merged_into}"
                    )
            if keep_account_id not in (account_a_id, account_b_id):
                raise InvalidKeepAccountError(
                    "保留账户必须是本次关联双方之一"
                )
            session = LinkSession(
                session_id=uuid.uuid4().hex,
                account_a_id=account_a_id,
                account_b_id=account_b_id,
                keep_account_id=keep_account_id,
                created_at=datetime.now(),
            )
            self._sessions[session.session_id] = session
            return session

    def submit_proof(
        self, session_id: str, proof: OwnershipProof, now: Optional[datetime] = None
    ) -> None:
        """提交一方的所有权证明。双方外部身份必须分别完成证明。"""
        now = now or datetime.now()
        with self._lock:
            session = self._get_session(session_id)
            self._ensure_not_final(session)
            proof.validate(now)
            owner_account = self._identity_account.get(proof.identity_id)
            if owner_account not in (session.account_a_id, session.account_b_id):
                raise ProofInvalidError(
                    f"identity {proof.identity_id!r} 不属于本次关联的双方账户"
                )
            session.proofs[proof.identity_id] = proof

    def confirm(self, session_id: str, now: Optional[datetime] = None) -> LinkSession:
        """确认合并：双方证明齐备且未过期、身份未被其他保留账户占用时生效。

        与 withdraw 互斥：谁先进入临界区谁生效，后到者收到
        SessionFinalizedError，最终只有一种结果。
        """
        now = now or datetime.now()
        with self._lock:
            session = self._get_session(session_id)
            self._ensure_not_final(session)

            required = self._required_identity_ids(session)
            if set(session.proofs) < required:
                raise SessionNotReadyError("双方外部身份尚未分别完成证明")
            for iid in required:
                session.proofs[iid].validate(now)  # 确认时刻再次校验有效期

            keep_id = session.keep_account_id
            source_id = (
                session.account_b_id
                if keep_id == session.account_a_id
                else session.account_a_id
            )
            # 外部身份已关联到其他保留账户 -> 阻止
            for iid in self._accounts[source_id].identity_ids:
                bound = self._identity_account.get(iid)
                if bound is not None and bound not in (source_id, keep_id):
                    raise IdentityAlreadyLinkedError(
                        f"identity {iid!r} 已关联到保留账户 {bound}"
                    )

            # 落账：迁移身份、标记来源账户、写来源记录
            for iid in list(self._accounts[source_id].identity_ids):
                self._identity_account[iid] = keep_id
                self._accounts[keep_id].identity_ids.add(iid)
                self._link_records.append(
                    LinkRecord(
                        identity_id=iid,
                        source_account_id=source_id,
                        retained_account_id=keep_id,
                        linked_at=now,
                    )
                )
            self._accounts[source_id].identity_ids.clear()
            self._accounts[source_id].merged_into = keep_id
            session.status = SessionStatus.CONFIRMED
            session.finalized_at = now
            return session

    def withdraw(
        self, session_id: str, by_identity_id: str, now: Optional[datetime] = None
    ) -> LinkSession:
        """任一方撤回。与 confirm 互斥，只产生一种最终结果。"""
        now = now or datetime.now()
        with self._lock:
            session = self._get_session(session_id)
            self._ensure_not_final(session)
            owner_account = self._identity_account.get(by_identity_id)
            if owner_account not in (session.account_a_id, session.account_b_id):
                raise ProofInvalidError(
                    f"identity {by_identity_id!r} 不属于本次关联的双方账户"
                )
            session.status = SessionStatus.WITHDRAWN
            session.finalized_at = now
            return session

    # ------------------------------------------------------------------
    # 登录解析
    # ------------------------------------------------------------------
    def resolve_login(self, identity_id: str) -> Tuple[str, List[LinkRecord]]:
        """通过原身份登录：解析到保留账户，并返回来源记录。"""
        with self._lock:
            account_id = self._identity_account.get(identity_id)
            if account_id is None:
                raise KeyError(f"外部身份不存在: {identity_id}")
            seen = set()
            while self._accounts[account_id].merged_into is not None:
                account_id = self._accounts[account_id].merged_into
                if account_id in seen:
                    raise RuntimeError("检测到合并环")
                seen.add(account_id)
            provenance = [
                r for r in self._link_records if r.retained_account_id == account_id
            ]
            return account_id, provenance

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _get_session(self, session_id: str) -> LinkSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(f"会话不存在: {session_id}")
        return session

    @staticmethod
    def _ensure_not_final(session: LinkSession) -> None:
        if session.is_final:
            raise SessionFinalizedError(
                f"会话已终态({session.status.value})，拒绝后续操作"
            )

    def _required_identity_ids(self, session: LinkSession) -> set:
        ids = set()
        for aid in (session.account_a_id, session.account_b_id):
            ids |= self._accounts[aid].identity_ids
        return ids
