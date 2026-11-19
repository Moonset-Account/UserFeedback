"""网关政策：算法强度等级注册表与 AEAD 算法集合。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .errors import UnknownAlgorithmError

# 代表性比特安全强度（NIST 惯例：2048 位 MODP 约 112 位等）。
DEFAULT_STRENGTHS: dict[str, int] = {
    # 加密算法
    "AES128-GCM": 128,
    "AES256-GCM": 256,
    "AES128-CBC": 128,
    "AES256-CBC": 256,
    "3DES-CBC": 112,
    # 完整性算法
    "HMAC-SHA1-96": 96,
    "HMAC-SHA256-128": 128,
    "HMAC-SHA384-192": 192,
    "HMAC-SHA512-256": 256,
    # 密钥交换组
    "DH2": 80,
    "DH14": 112,
    "DH19": 128,
    "DH20": 192,
    "DH21": 256,
}

# 自带认证的 AEAD 加密算法，不能另配完整性算法。
DEFAULT_AEAD_ENCRYPTIONS: frozenset[str] = frozenset(
    {"AES128-GCM", "AES256-GCM"}
)


@dataclass(frozen=True)
class Policy:
    """网关协商政策。

    ``strengths`` 给出每个算法/分组的强度等级；套件强度取各组件最小值。
    ``aead_encryptions`` 标记内置认证的加密算法。
    ``min_strength`` 是当前协商（含换钥生效门槛）的最低强度要求。
    """

    strengths: Mapping[str, int]
    aead_encryptions: frozenset[str]
    min_strength: int = 0

    def __post_init__(self):
        if not isinstance(self.strengths, dict):
            object.__setattr__(self, "strengths", dict(self.strengths))
        if not isinstance(self.aead_encryptions, frozenset):
            object.__setattr__(
                self, "aead_encryptions", frozenset(self.aead_encryptions)
            )
        if self.min_strength < 0:
            raise ValueError("min_strength 不能为负数")

    def is_aead(self, encryption: str) -> bool:
        return encryption in self.aead_encryptions

    def strength_of(self, algorithm: str) -> int:
        try:
            return self.strengths[algorithm]
        except KeyError as exc:
            raise UnknownAlgorithmError(f"政策中未登记的算法/分组: {algorithm!r}") from exc

    def suite_strength(
        self, encryption: str, integrity: str | None, group: str
    ) -> int:
        """套件强度由最弱组件决定；AEAD 套件没有独立完整性组件。"""
        values = [self.strength_of(encryption), self.strength_of(group)]
        if integrity is not None:
            values.append(self.strength_of(integrity))
        return min(values)


def default_policy(min_strength: int = 0) -> Policy:
    """构造内置算法注册表的默认政策，可指定最低强度门槛。"""
    return Policy(
        strengths=dict(DEFAULT_STRENGTHS),
        aead_encryptions=DEFAULT_AEAD_ENCRYPTIONS,
        min_strength=min_strength,
    )
