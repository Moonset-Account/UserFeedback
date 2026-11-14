"""Trusted identity stub for SEC-VULN-043.

Provides subjects, roles and tenants without any external identity
system. The stub is "trusted": it is the only way to mint an
ApprovalRecord inside the scenario suite, and it enforces that the
subject exists, actually holds the claimed role, and acts inside its
own tenant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable

from approval_core import ApprovalRecord


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: frozenset
    tenant: str


class IdentityStub:
    def __init__(self) -> None:
        self._principals: Dict[str, Principal] = {}

    def register(self, subject: str, roles: Iterable[str], tenant: str) -> None:
        self._principals[subject] = Principal(subject, frozenset(roles), tenant)

    def principal(self, subject: str) -> Principal:
        try:
            return self._principals[subject]
        except KeyError:
            raise PermissionError(f"unknown subject: {subject}") from None

    def issue_approval(self, subject: str, role: str, *, record_id: str,
                       tenant: str, resource_id: str, resource_revision: int,
                       operation: str, params: Any, decision: str,
                       expires_at: int) -> ApprovalRecord:
        """Mint an approval record after authenticating the subject and
        authorizing the claimed role within the subject's tenant."""
        principal = self.principal(subject)
        if role not in principal.roles:
            raise PermissionError(
                f"subject {subject} does not hold role {role}")
        if principal.tenant != tenant:
            raise PermissionError(
                f"subject {subject} belongs to tenant {principal.tenant}, "
                f"cannot approve in tenant {tenant}")
        if decision not in ("approve", "reject"):
            raise ValueError(f"invalid decision: {decision}")
        return ApprovalRecord(
            record_id=record_id,
            tenant=tenant,
            resource_id=resource_id,
            resource_revision=resource_revision,
            operation=operation,
            params=params,
            approver=subject,
            role=role,
            decision=decision,
            expires_at=expires_at,
        )
