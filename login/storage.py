"""本地模拟凭据存储。

每个用户一个 JSON 文件，凭据记录中持久化算法（``algorithm``）与参数版本
（``param_version``），以及用于乐观并发控制的 ``record_version``。

写入采用“临时文件 + ``os.replace``”的原子替换方式：升级中断（例如写入前
钩子抛异常、进程崩溃在 ``replace`` 之前）不会留下半条记录，旧记录仍可用于
重新验证与重试。

:meth:`JsonFileCredentialStore.compare_and_swap` 只在磁盘上的
``record_version`` 与调用方持有的期望值一致时才覆盖记录，因此并发的旧哈希
升级最多只有一次能成功落盘，也不会覆盖用户刚刚改过的新口令记录。
"""

from __future__ import annotations

import json
import os
import re
import threading
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Optional

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _validate_user_id(user_id: str) -> None:
    if not isinstance(user_id, str) or not _SAFE_ID.match(user_id):
        raise ValueError("user_id 只允许字母、数字、下划线与连字符，长度 1-128")


@dataclass
class CredentialRecord:
    """一条可持久化的凭据记录。"""

    user_id: str
    algorithm: str
    param_version: str
    iterations: int
    salt_hex: str
    hash_hex: str
    disabled: bool = False
    record_version: int = 1
    updated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CredentialRecord":
        return cls(
            user_id=data["user_id"],
            algorithm=data["algorithm"],
            param_version=data["param_version"],
            iterations=int(data["iterations"]),
            salt_hex=data["salt_hex"],
            hash_hex=data["hash_hex"],
            disabled=bool(data.get("disabled", False)),
            record_version=int(data.get("record_version", 1)),
            updated_at=str(data.get("updated_at", "")),
        )

    def hash_fields(self) -> dict:
        """提取哈希验证所需字段（算法与参数版本由记录自身携带）。"""
        return {
            "algorithm": self.algorithm,
            "param_version": self.param_version,
            "iterations": self.iterations,
            "salt_hex": self.salt_hex,
            "hash_hex": self.hash_hex,
        }


class CredentialStore(ABC):
    """凭据存储的抽象接口，便于替换为内存实现或其他后端。"""

    @abstractmethod
    def get(self, user_id: str) -> Optional[CredentialRecord]:
        """读取记录；不存在时返回 ``None``。"""

    @abstractmethod
    def save_new(self, record: CredentialRecord) -> None:
        """插入新记录；若用户已存在则拒绝。"""

    @abstractmethod
    def compare_and_swap(
        self,
        user_id: str,
        expected_record_version: int,
        new_record: CredentialRecord,
    ) -> CredentialRecord:
        """乐观锁更新，版本不匹配时抛出 :class:`ConcurrentModification`。"""


class JsonFileCredentialStore(CredentialStore):
    """以 JSON 文件作为本地模拟存储。"""

    def __init__(self, base_dir: str):
        self.base_dir = os.path.abspath(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)
        # 进程内串行化同一存储上的读改写，跨进程由原子替换 + CAS 兜底。
        self._lock = threading.RLock()

    def _path(self, user_id: str) -> str:
        _validate_user_id(user_id)
        return os.path.join(self.base_dir, f"{user_id}.json")

    def _read_unlocked(self, user_id: str) -> Optional[CredentialRecord]:
        path = self._path(user_id)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return CredentialRecord.from_dict(json.load(handle))

    def get(self, user_id: str) -> Optional[CredentialRecord]:
        with self._lock:
            return self._read_unlocked(user_id)

    def save_new(self, record: CredentialRecord) -> None:
        _validate_user_id(record.user_id)
        with self._lock:
            path = self._path(record.user_id)
            if os.path.exists(path):
                raise FileExistsError(f"凭据记录已存在: {record.user_id}")
            self._atomic_write_unlocked(path, record)

    def compare_and_swap(
        self,
        user_id: str,
        expected_record_version: int,
        new_record: CredentialRecord,
    ) -> CredentialRecord:
        from .exceptions import ConcurrentModification

        _validate_user_id(user_id)
        with self._lock:
            current = self._read_unlocked(user_id)
            if current is None:
                raise ConcurrentModification(
                    f"记录已不存在，拒绝升级: {user_id}"
                )
            if current.record_version != expected_record_version:
                raise ConcurrentModification(
                    "记录版本已变化 "
                    f"(期望 {expected_record_version}，实际 {current.record_version})，"
                    "拒绝覆盖新记录"
                )
            stored = CredentialRecord(
                user_id=user_id,
                algorithm=new_record.algorithm,
                param_version=new_record.param_version,
                iterations=new_record.iterations,
                salt_hex=new_record.salt_hex,
                hash_hex=new_record.hash_hex,
                disabled=new_record.disabled,
                record_version=expected_record_version + 1,
                updated_at=new_record.updated_at,
            )
            self._atomic_write_unlocked(self._path(user_id), stored)
            return stored

    @staticmethod
    def _atomic_write_unlocked(path: str, record: CredentialRecord) -> None:
        tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(record.to_dict(), handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
