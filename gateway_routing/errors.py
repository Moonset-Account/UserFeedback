"""Exception hierarchy for the gateway routing library."""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from .router import Conflict


class RoutingError(Exception):
    """Base class for every error raised by this package."""


class TemplateError(RoutingError, ValueError):
    """A path template is not syntactically valid."""

    def __init__(self, template: object, reason: str) -> None:
        self.template = template
        self.reason = reason
        super().__init__(f"invalid path template {template!r}: {reason}")


class PathError(RoutingError, ValueError):
    """A request path is not a canonical path understood by the router."""

    def __init__(self, path: object, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"invalid request path {path!r}: {reason}")


class PublishError(RoutingError):
    """A whole publication batch was rejected; the previous table is intact."""


class RouteConflictError(PublishError):
    """At least two rules overlap without one set strictly containing the other."""

    def __init__(self, conflicts: Iterable["Conflict"]) -> None:
        self.conflicts = tuple(conflicts)
        noun = "conflict" if len(self.conflicts) == 1 else "conflicts"
        lines = [
            f"{len(self.conflicts)} route {noun}: publication rejected, "
            "previous routing table retained"
        ]
        for conflict in self.conflicts:
            lines.append(f"- [{conflict.method}] {conflict.describe()}")
        super().__init__("\n".join(lines))

    @property
    def methods(self) -> tuple[str, ...]:
        return tuple(sorted({conflict.method for conflict in self.conflicts}))

    def for_method(self, method: str) -> tuple["Conflict", ...]:
        normalized = method.strip().upper()
        return tuple(c for c in self.conflicts if c.method == normalized)
