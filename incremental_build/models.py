"""Public result and record types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .versions import VersionStamp


@dataclass(frozen=True)
class ReadRecord:
    """One actual file read made by an action callback."""

    path: str
    version: VersionStamp
    sequence: int

    @property
    def revision(self) -> int:
        return self.version.revision


@dataclass
class StepSpec:
    name: str
    action: Any
    outputs: frozenset[str] = field(default_factory=frozenset)
    static_dependencies: frozenset[str] = field(default_factory=frozenset)
    depends_on: frozenset[str] = field(default_factory=frozenset)


@dataclass
class AttemptRecord:
    attempt: int
    status: str  # published | stale | failed | missing_outputs
    reads: list[ReadRecord] = field(default_factory=list)
    stale_reads: list[ReadRecord] = field(default_factory=list)
    error_type: str | None = None
    error: str | None = None
    traceback: str | None = None

    @property
    def published(self) -> bool:
        return self.status == "published"


@dataclass
class StepRun:
    name: str
    status: str  # ran | cached | failed | skipped | cycle
    reason: str | None = None
    attempts: list[AttemptRecord] = field(default_factory=list)
    dynamic_dependencies: dict[str, VersionStamp] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {"ran", "cached"}

    @property
    def published(self) -> bool:
        return self.status == "ran" and any(a.published for a in self.attempts)

    @property
    def attempt_count(self) -> int:
        return len(self.attempts) if self.attempts else 1

    @property
    def retried(self) -> bool:
        return len(self.attempts) > 1


@dataclass
class BuildResult:
    targets: list[str]
    steps: list[StepRun] = field(default_factory=list)
    cycle_error: Exception | None = None

    @property
    def ok(self) -> bool:
        return self.cycle_error is None and all(step.ok for step in self.steps)

    def step(self, name: str) -> StepRun:
        for run in self.steps:
            if run.name == name:
                return run
        raise KeyError(name)

    @property
    def ran_steps(self) -> list[str]:
        return [run.name for run in self.steps if run.status == "ran"]

    @property
    def cached_steps(self) -> list[str]:
        return [run.name for run in self.steps if run.status == "cached"]

    @property
    def failed_steps(self) -> list[str]:
        return [run.name for run in self.steps if run.status in {"failed", "cycle"}]

    @property
    def skipped_steps(self) -> list[str]:
        return [run.name for run in self.steps if run.status == "skipped"]

    def raise_for_status(self) -> None:
        if self.cycle_error is not None:
            raise self.cycle_error
        if not self.ok:
            from .errors import BuildFailed

            raise BuildFailed(self)
