"""审计记录。

每条记录包含：事件、判定结果（allow/deny）、原因码、租户/用户/会话、
挑战/许可标识、导出范围与时间戳。

安全约束：审计记录绝不包含敏感验证凭证（SSO token、MFA proof），
这些凭证只留在服务端适配器内部。
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    ts: float
    event: str  # initiate_mfa | confirm_challenge | redeem_license
    decision: str  # allow | deny
    reason: str  # 见 service.Reason
    tenant_id: Optional[str]
    user_id: Optional[str]
    session_id: Optional[str]
    challenge_id: Optional[str]
    license_id: Optional[str]
    scope: Optional[str]

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


class AuditLog:
    """线程安全的内存审计日志。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: List[AuditRecord] = []

    def append(
        self,
        *,
        ts: float,
        event: str,
        decision: str,
        reason: str,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        challenge_id: Optional[str] = None,
        license_id: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> AuditRecord:
        with self._lock:
            record = AuditRecord(
                seq=len(self._records) + 1,
                ts=ts,
                event=event,
                decision=decision,
                reason=reason,
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                challenge_id=challenge_id,
                license_id=license_id,
                scope=scope,
            )
            self._records.append(record)
            return record

    def records(self) -> List[AuditRecord]:
        with self._lock:
            return list(self._records)

    def to_list(self) -> List[Dict[str, object]]:
        return [r.to_dict() for r in self.records()]
