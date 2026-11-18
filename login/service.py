"""登录验证与哈希参数升级服务。

关键约束（对应题面）：

1. 兼容旧参数哈希：按记录自身的 ``algorithm`` / ``param_version`` 验证；
   一次成功验证后，若参数版本低于本地当前配置，则升级为新参数并持久化。
2. 验证期间用户改了口令：旧口令本次不能建立会话，旧哈希的升级也不能覆盖
   新记录。实现上先验证再取锁，取锁后重新读取并比较记录版本，版本变化则
   对新记录重新验证；仍不匹配就抛出 :class:`CredentialModifiedDuringLogin`，
   且升级写入通过存储层 CAS 再次兜底。
3. 两个并发登录只升级一次：每用户锁串行化升级临界区，第二个登录在锁内重读，
   发现已是当前参数后跳过升级。
4. 账户停用稳定拒绝：验证前与升级临界区内各检查一次 ``disabled``。
5. 升级中断可重试：升级阶段的异常统一转换为
   :class:`UpgradeInterrupted`；存储使用原子写，中断不破坏旧记录，下次可用
   有效记录重新验证并再次尝试升级。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .exceptions import (
    AccountDisabled,
    ConcurrentModification,
    CredentialModifiedDuringLogin,
    InvalidPassword,
    UpgradeInterrupted,
    UserNotFound,
)
from .hashing import (
    ALGORITHM,
    CURRENT_PARAM_VERSION,
    hash_password,
    verify_password,
)
from .storage import CredentialRecord, CredentialStore


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LoginTracer:
    """测试与审计用的钩子，默认全部为空操作。

    所有方法都在具体时机被 :class:`LoginService` 调用；测试可通过抛异常来
    模拟“升级中断”，或计数来验证并发登录只发生一次升级写入。
    """

    def password_verified(self, user_id: str, param_version: str) -> None:
        """口令按记录参数验证通过（此时尚未建立会话、尚未升级）。"""

    def before_upgrade_write(self, user_id: str, from_version: str, to_version: str) -> None:
        """新哈希已计算、即将 CAS 落盘前；抛异常可模拟升级中断。"""

    def after_upgrade_committed(self, user_id: str, new_record_version: int) -> None:
        """升级记录已成功提交到存储。"""

    def upgrade_skipped(self, user_id: str, reason: str) -> None:
        """本次登录没有执行升级写入（已是当前参数或被并发登录抢先升级）。"""


@dataclass(frozen=True)
class Session:
    """成功验证后建立的会话。"""

    session_id: str
    user_id: str
    param_version: str
    record_version: int
    created_at: str


class LoginService:
    def __init__(self, store: CredentialStore, tracer: Optional[LoginTracer] = None):
        self.store = store
        self.tracer = tracer or LoginTracer()
        # 每用户一把锁：并发登录在同一用户的升级临界区上串行。
        self._user_locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, user_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._user_locks.get(user_id)
            if lock is None:
                lock = threading.RLock()
                self._user_locks[user_id] = lock
            return lock

    # ------------------------------------------------------------------ 注册

    def register(
        self,
        user_id: str,
        password: str,
        param_version: str = CURRENT_PARAM_VERSION,
        disabled: bool = False,
    ) -> CredentialRecord:
        """创建一条凭据记录，默认使用当前参数。"""
        fields = hash_password(password, param_version)
        record = CredentialRecord(
            user_id=user_id,
            algorithm=fields["algorithm"],
            param_version=fields["param_version"],
            iterations=fields["iterations"],
            salt_hex=fields["salt_hex"],
            hash_hex=fields["hash_hex"],
            disabled=disabled,
            record_version=1,
            updated_at=_utc_now(),
        )
        self.store.save_new(record)
        return record

    # ------------------------------------------------------------------ 登录

    def login(self, user_id: str, password: str) -> Session:
        """验证口令并在需要时透明升级哈希参数，成功则返回会话。"""
        record = self.store.get(user_id)
        if record is None:
            raise UserNotFound(f"用户不存在: {user_id}")
        if record.disabled:
            # 停用账户在任何昂贵操作之前即被拒绝，且结果稳定。
            raise AccountDisabled(f"账户已停用: {user_id}")

        # 1) 按记录携带的旧参数验证（兼容旧哈希）。此阶段不持锁，
        #    因此验证期间另一个线程改口令不会被阻塞。
        if not verify_password(password, record.hash_fields()):
            raise InvalidPassword("口令验证失败")
        self.tracer.password_verified(user_id, record.param_version)
        verified_record_version = record.record_version

        user_lock = self._lock_for(user_id)
        with user_lock:
            # 2) 重新读取，检测验证期间凭据是否被修改（改口令/停用）。
            current = self.store.get(user_id)
            if current is None:
                raise CredentialModifiedDuringLogin("验证期间凭据记录被删除，拒绝建立会话")
            if current.disabled:
                raise AccountDisabled(f"账户已停用: {user_id}")

            if current.record_version != verified_record_version:
                # 验证期间记录发生了变化。对新记录重新验证当前提交的口令：
                # 通过则继续，但绝不使用旧哈希去覆盖新记录；
                # 不通过则本次登录拒绝建立会话。
                if not verify_password(password, current.hash_fields()):
                    raise CredentialModifiedDuringLogin(
                        "验证期间口令已被修改，旧口令不得建立会话"
                    )
                record = current
                if record.param_version == CURRENT_PARAM_VERSION:
                    self.tracer.upgrade_skipped(user_id, "记录已在验证期间升级为当前参数")
                    return self._new_session(record)
            else:
                record = current

            # 3) 已是当前参数：无需升级，直接建立会话。
            if record.param_version == CURRENT_PARAM_VERSION:
                self.tracer.upgrade_skipped(user_id, "already_current")
                return self._new_session(record)

            # 4) 计算新参数哈希并通过 CAS 落盘。CAS 保证旧升级不会覆盖
            #    锁外并发写入的新记录（存储层最后一道防线）。
            try:
                new_fields = hash_password(password, CURRENT_PARAM_VERSION)
                upgraded = CredentialRecord(
                    user_id=user_id,
                    algorithm=ALGORITHM,
                    param_version=new_fields["param_version"],
                    iterations=new_fields["iterations"],
                    salt_hex=new_fields["salt_hex"],
                    hash_hex=new_fields["hash_hex"],
                    disabled=record.disabled,
                    record_version=record.record_version,
                    updated_at=_utc_now(),
                )
                self.tracer.before_upgrade_write(
                    user_id, record.param_version, CURRENT_PARAM_VERSION
                )
                committed = self.store.compare_and_swap(
                    user_id, record.record_version, upgraded
                )
            except ConcurrentModification:
                # 并发登录已抢先升级，或用户恰好改了口令。重新读取裁定：
                # 新记录仍能验证当前口令则放行，否则拒绝建立会话。
                latest = self.store.get(user_id)
                if latest is None or latest.disabled:
                    raise CredentialModifiedDuringLogin(
                        "升级落盘时记录已变更，拒绝建立会话"
                    )
                if not verify_password(password, latest.hash_fields()):
                    raise CredentialModifiedDuringLogin(
                        "升级落盘时口令已被修改，旧口令不得建立会话"
                    )
                self.tracer.upgrade_skipped(user_id, "lost_cas_race")
                return self._new_session(latest)
            except Exception as exc:
                # 升级中断：原子写保证磁盘旧记录完好，可在下次登录重试。
                raise UpgradeInterrupted(
                    f"哈希参数升级中断，原记录保持不变，可重试: {exc}"
                ) from exc

            self.tracer.after_upgrade_committed(user_id, committed.record_version)
            return self._new_session(committed)

    @staticmethod
    def _new_session(record: CredentialRecord) -> Session:
        return Session(
            session_id=uuid.uuid4().hex,
            user_id=record.user_id,
            param_version=record.param_version,
            record_version=record.record_version,
            created_at=_utc_now(),
        )

    # ------------------------------------------------------------- 改口令/停用

    def change_password(self, user_id: str, new_password: str) -> CredentialRecord:
        """修改口令并使用当前参数重新哈希，带乐观锁重试。

        与进行中的登录并发时，本方法会推进 ``record_version``，使对方基于旧
        记录的升级 CAS 失败，从而保证新记录不被旧哈希覆盖。
        """
        user_lock = self._lock_for(user_id)
        with user_lock:
            record = self.store.get(user_id)
            if record is None:
                raise UserNotFound(f"用户不存在: {user_id}")
            fields = hash_password(new_password, CURRENT_PARAM_VERSION)
            updated = CredentialRecord(
                user_id=user_id,
                algorithm=fields["algorithm"],
                param_version=fields["param_version"],
                iterations=fields["iterations"],
                salt_hex=fields["salt_hex"],
                hash_hex=fields["hash_hex"],
                disabled=record.disabled,
                record_version=record.record_version,
                updated_at=_utc_now(),
            )
            return self.store.compare_and_swap(user_id, record.record_version, updated)

    def set_disabled(self, user_id: str, disabled: bool) -> CredentialRecord:
        """启用/停用账户，带乐观锁重试。"""
        user_lock = self._lock_for(user_id)
        with user_lock:
            record = self.store.get(user_id)
            if record is None:
                raise UserNotFound(f"用户不存在: {user_id}")
            updated = CredentialRecord(
                user_id=record.user_id,
                algorithm=record.algorithm,
                param_version=record.param_version,
                iterations=record.iterations,
                salt_hex=record.salt_hex,
                hash_hex=record.hash_hex,
                disabled=disabled,
                record_version=record.record_version,
                updated_at=_utc_now(),
            )
            return self.store.compare_and_swap(user_id, record.record_version, updated)
