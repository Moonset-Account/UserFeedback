"""口令哈希与参数版本管理。

存储记录里的哈希采用 PBKDF2-HMAC-SHA256。不同的迭代次数通过
``param_version`` 区分：旧参数（``v1``）的记录在一次成功验证后会被
:class:`~login.service.LoginService` 升级为当前参数（``v2``）。

记录中同时持久化 ``algorithm`` 与 ``param_version``，使存储自身可以解释
每条记录使用的算法与参数版本，而不依赖进程内的隐式约定。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

ALGORITHM = "pbkdf2_sha256"

# 盐长度（字节），hex 编码后落盘。
SALT_BYTES = 16


@dataclass(frozen=True)
class HashParams:
    """一组可持久化解释的哈希参数。"""

    param_version: str
    algorithm: str
    iterations: int
    digest_name: str

    def derive(self, password: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac(
            self.digest_name,
            password.encode("utf-8"),
            salt,
            self.iterations,
        )


# v1 模拟“旧参数”：迭代次数较低；v2 是本地配置的当前参数。
PARAM_SETS: dict[str, HashParams] = {
    "v1": HashParams(
        param_version="v1",
        algorithm=ALGORITHM,
        iterations=50_000,
        digest_name="sha256",
    ),
    "v2": HashParams(
        param_version="v2",
        algorithm=ALGORITHM,
        iterations=200_000,
        digest_name="sha256",
    ),
}

CURRENT_PARAM_VERSION = "v2"


def _params_for(algorithm: str, param_version: str) -> HashParams:
    if algorithm != ALGORITHM:
        raise ValueError(f"不支持的哈希算法: {algorithm!r}")
    try:
        return PARAM_SETS[param_version]
    except KeyError as exc:
        raise ValueError(f"未知的参数版本: {param_version!r}") from exc


def hash_password(password: str, param_version: str = CURRENT_PARAM_VERSION) -> dict:
    """按指定参数版本生成口令哈希，返回可直接持久化的字段字典。"""
    params = _params_for(ALGORITHM, param_version)
    salt = secrets.token_bytes(SALT_BYTES)
    digest = params.derive(password, salt)
    return {
        "algorithm": params.algorithm,
        "param_version": params.param_version,
        "iterations": params.iterations,
        "salt_hex": salt.hex(),
        "hash_hex": digest.hex(),
    }


def verify_password(password: str, stored: dict) -> bool:
    """按记录自身携带的算法与参数验证口令，使用恒定时间比较。"""
    try:
        params = _params_for(stored["algorithm"], stored["param_version"])
        salt = bytes.fromhex(stored["salt_hex"])
        expected = bytes.fromhex(stored["hash_hex"])
    except (KeyError, TypeError, ValueError):
        return False
    candidate = params.derive(password, salt)
    return hmac.compare_digest(candidate, expected)
