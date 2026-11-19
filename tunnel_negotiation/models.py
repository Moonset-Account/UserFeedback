"""隧道协商的数据模型：有序提案与协商结果。"""

from __future__ import annotations

from dataclasses import dataclass


def _normalize(values, field_name: str, *, allow_empty: bool) -> list[str]:
    if values is None:
        values = []
    if isinstance(values, str):
        values = [values]
    normalized = list(values)
    if not normalized and not allow_empty:
        raise ValueError(f"{field_name} 至少需要包含一个算法")
    seen: set[str] = set()
    for value in normalized:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field_name} 的元素必须是非空字符串")
        if value in seen:
            raise ValueError(f"{field_name} 中存在重复算法 {value!r}")
        seen.add(value)
    return normalized


@dataclass
class Proposal:
    """一份自包含的有序提案。

    完整套件的加密、完整性、密钥交换组必须来自同一份提案，
    不同提案的算法清单严禁互相拼接。各清单顺序即本端偏好（高优先在前）。
    AEAD（自带认证）加密提案的 ``integrity_algorithms`` 应为空清单。
    """

    proposal_id: int
    encryption_algorithms: list[str]
    integrity_algorithms: list[str]
    key_exchange_groups: list[str]

    def __post_init__(self):
        if isinstance(self.proposal_id, bool) or not isinstance(self.proposal_id, int):
            raise ValueError("proposal_id 必须是整数")
        self.encryption_algorithms = _normalize(
            self.encryption_algorithms, "encryption_algorithms", allow_empty=False
        )
        # 仅 AEAD 提案允许完整性清单为空，语义合法性在协商阶段校验。
        self.integrity_algorithms = _normalize(
            self.integrity_algorithms, "integrity_algorithms", allow_empty=True
        )
        self.key_exchange_groups = _normalize(
            self.key_exchange_groups, "key_exchange_groups", allow_empty=False
        )


@dataclass(frozen=True)
class SelectedSuite:
    """唯一选定的协商结果。"""

    encryption: str
    integrity: str | None
    group: str
    strength: int
    local_proposal_id: int
    remote_proposal_id: int


@dataclass(frozen=True)
class RejectedSuite:
    """因强度不足被政策淘汰的候选套件。"""

    encryption: str
    integrity: str | None
    group: str
    strength: int
    local_proposal_id: int
    remote_proposal_id: int
    reason: str


@dataclass(frozen=True)
class PairDiagnostic:
    """一对提案无法形成共同套件的结构化诊断信息。"""

    local_proposal_id: int
    remote_proposal_id: int
    common_encryption: tuple[str, ...]
    common_integrity: tuple[str, ...]
    common_groups: tuple[str, ...]
    reason: str
