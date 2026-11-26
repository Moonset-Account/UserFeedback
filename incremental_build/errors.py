"""Errors raised by the incremental build coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class BuildCoordinationError(Exception):
    """Base class for coordinator usage and graph errors."""


class DuplicateProducerError(BuildCoordinationError):
    """Raised when two steps produce the same output path."""


class UnknownStepError(BuildCoordinationError):
    """Raised when a target or dependency step is not registered."""


class UnknownOutputError(BuildCoordinationError):
    """Raised when a target path has no registered producer."""


class UndeclaredOutputError(BuildCoordinationError):
    """An action tried to stage an output not declared for its step."""


@dataclass(frozen=True)
class CycleEdge:
    """One edge in a dependency cycle."""

    source: str
    target: str
    path: str | None = None
    kind: str = "dynamic"
    newly_added: bool = False

    def format(self) -> str:
        label = f"{self.source} -> {self.target}"
        details: list[str] = [self.kind]
        if self.path is not None:
            details.append(self.path)
        if self.newly_added:
            details.append("newly reported")
        return f"{label} ({'; '.join(details)})"


class DependencyCycleError(BuildCoordinationError):
    """A static or newly reported dynamic dependency closes a cycle."""

    def __init__(
        self,
        cycle: list[str] | tuple[str, ...],
        edges: list[CycleEdge] | tuple[CycleEdge, ...] | None = None,
    ) -> None:
        self.cycle = list(cycle)
        self.edges = list(edges or [])
        closed_loop = " -> ".join(self.cycle)
        message = f"Dependency cycle detected: {closed_loop}"
        if self.edges:
            message += "\nClosed-loop edges:\n  " + "\n  ".join(
                edge.format() for edge in self.edges
            )
        message += (
            "\nNo new manifest or output publication was committed; "
            "previous build artifacts were retained."
        )
        super().__init__(message)


class StaleReadError(BuildCoordinationError):
    """Raised internally/reported when read versions no longer match."""

    def __init__(self, stale_reads: list[Any]) -> None:
        self.stale_reads = list(stale_reads)
        paths = ", ".join(sorted({record.path for record in self.stale_reads}))
        super().__init__(f"Action read stale input versions: {paths}")


class BuildFailed(BuildCoordinationError):
    """Raised by ``BuildResult.raise_for_status`` / strict runs."""

    def __init__(self, result: Any) -> None:
        self.result = result
        failed = ", ".join(result.failed_steps) or "unknown step"
        super().__init__(f"Incremental build failed for step(s): {failed}")
