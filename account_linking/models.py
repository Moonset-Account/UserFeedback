"""领域模型。"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional


class SessionStatus(enum.Enum):
    """关联会话状态机。

    PENDING -> CONFIRMED | WITHDRAWN  为终态迁移，且只允许发生一次。
    """

    PENDING = "pending"
    CONFIRMED = "confirmed"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class ExternalIdentity:
    """外部身份（如某机构 IdP 下发的账号标识）。"""

    identity_id: str
    provider: str
    subject: str


@dataclass
class Account:
    """应用内账户。"""

    account_id: str
    email: str
    identity_ids: set = field(default_factory=set)
    merged_into: Optional[str] = None  # 若非空，表示该账户已并入指定保留账户


@dataclass(frozen=True)
class LinkRecord:
    """来源记录：某外部身份于何时并入哪个保留账户。"""

    identity_id: str
    source_account_id: str
    retained_account_id: str
    linked_at: datetime


@dataclass
class LinkSession:
    """一次账户关联会话。"""

    session_id: str
    account_a_id: str
    account_b_id: str
    keep_account_id: str
    created_at: datetime
    status: SessionStatus = SessionStatus.PENDING
    # identity_id -> 已接受的证明
    proofs: Dict[str, object] = field(default_factory=dict)
    finalized_at: Optional[datetime] = None

    @property
    def is_final(self) -> bool:
        return self.status in (SessionStatus.CONFIRMED, SessionStatus.WITHDRAWN)
