"""Public error types for the dual-version backend store."""

from __future__ import annotations

from typing import Any, Iterable, Mapping


class DualVersionError(Exception):
    """Base class for every error raised by :mod:`dualversion`."""


class ValidationError(DualVersionError, ValueError):
    """The payload is malformed or violates a field constraint.

    Validation runs before any state is changed, so it never leaves a partial
    write behind.
    """


class WriteConflict(DualVersionError):
    """An optimistic-concurrency check failed; nothing was written.

    Parameters
    ----------
    reason:
        ``"field_modified"`` (another write changed a field this request also
        changes), ``"target_deleted"`` (a referenced backend no longer exists),
        ``"idempotency_key_reuse"`` (a request id was replayed with a different
        payload), or ``"multiple_conflicts"`` for a mixed batch.
    targets:
        One mapping per conflicting target.  Each has at least ``id`` and
        ``reason``; field conflicts also carry ``field``.
    resource_version:
        The store resource version at the moment the write was rejected.

    ``target_ids`` lists the conflicting backend ids, de-duplicated and sorted.
    """

    def __init__(
        self,
        reason: str,
        targets: Iterable[Mapping[str, Any]] | None = None,
        resource_version: int | None = None,
    ) -> None:
        self.reason = reason
        self.targets = tuple(dict(target) for target in (targets or ()))
        self.resource_version = resource_version
        ids = ", ".join(self.target_ids) or "-"
        super().__init__(f"write conflict ({reason}); targets: {ids}")

    @property
    def target_ids(self) -> tuple[str, ...]:
        """Sorted, de-duplicated ids of every conflicting target."""

        return tuple(sorted({str(t["id"]) for t in self.targets if t.get("id")}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict": True,
            "reason": self.reason,
            "targetIds": list(self.target_ids),
            "targets": [dict(target) for target in self.targets],
            "resourceVersion": self.resource_version,
        }
