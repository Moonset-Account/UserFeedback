"""Exceptions raised by the sandbox permission model."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .policy import MovePlan


class SandboxError(Exception):
    """Base class for all model errors."""


class InvalidPathError(SandboxError):
    """A virtual path is malformed or does not identify a node."""


class NodeNotFoundError(InvalidPathError):
    """A path or stable inode does not exist."""


class PermissionDeniedError(SandboxError):
    """The current policy denies an operation."""

    def __init__(self, message: str, *, reasons: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.reasons = reasons


class MoveError(SandboxError):
    """The requested subtree move is structurally invalid."""


class MoveDeniedError(PermissionDeniedError):
    """Base class for rejected moves."""


class MoveEndpointDeniedError(MoveDeniedError):
    """A source or destination endpoint lacks its move permission."""


class MoveEscalationError(MoveDeniedError):
    """The move would give some moved object a new r/w/x right."""

    def __init__(self, plan: "MovePlan") -> None:
        self.plan = plan
        self.gains = tuple(plan.gains)
        messages = [
            f"{gain.principal} gains {sorted(p.value for p in gain.added)} "
            f"on {gain.path_after} (inode={gain.inode})"
            for gain in plan.gains
        ]
        super().__init__(
            "Move would escalate permissions: " + "; ".join(messages),
            reasons=tuple(messages),
        )
