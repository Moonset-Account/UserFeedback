"""Action callback context and pause events."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from typing import TYPE_CHECKING, Any, Callable

from .errors import UndeclaredOutputError
from .models import ReadRecord, StepSpec
from .versions import VersionStamp

if TYPE_CHECKING:
    from .coordinator import BuildCoordinator


@dataclass
class PauseEvent:
    """Passed to a user pause hook when an action reaches a checkpoint."""

    coordinator: "BuildCoordinator"
    session: Any
    step: str
    attempt: int
    reason: str
    context: "BuildContext"

    def run_step(self, name: str) -> Any:
        """Run ``name`` to a verified state in the current build session."""

        return self.session.ensure(name)

    def write_file(self, path: str, data: bytes | str) -> VersionStamp:
        return self.coordinator.write_file(path, data)


class PauseGate:
    """Threading gate useful for deterministic real-interleaving tests.

    Use as ``run(..., pause_hook=gate)`` in a worker thread.  The controlling
    thread calls :meth:`wait_until_paused`, inspects ``gate.event`` and may call
    ``gate.event.run_step(...)`` before :meth:`resume`.  After resume, the gate
    stays open, so later attempts pass through without blocking.
    """

    def __init__(
        self,
        step: str | None = None,
        reason: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.step = step
        self.reason = reason
        self.timeout = timeout
        self.event: PauseEvent | None = None
        self.reached = Event()
        self.release = Event()

    def __call__(self, event: PauseEvent) -> None:
        if self.step is not None and event.step != self.step:
            return
        if self.reason is not None and event.reason != self.reason:
            return
        self.event = event
        self.reached.set()
        if not self.release.wait(self.timeout):
            raise TimeoutError(
                f"pause gate for {event.step}:{event.reason} was not resumed"
            )

    def wait_until_paused(self, timeout: float | None = None) -> PauseEvent:
        if not self.reached.wait(self.timeout if timeout is None else timeout):
            raise TimeoutError("timed out waiting for action to pause")
        if self.event is None:
            raise RuntimeError("pause gate reached without an event")
        return self.event

    def resume(self) -> None:
        self.release.set()


class BuildContext:
    """API exposed to a pausable build action."""

    def __init__(
        self,
        coordinator: "BuildCoordinator",
        spec: StepSpec,
        attempt: int,
        pause_hook: Callable[[PauseEvent], Any] | None,
        session: Any,
    ) -> None:
        self._coordinator = coordinator
        self._spec = spec
        self._attempt = attempt
        self._pause_hook = pause_hook
        self._session = session
        self._reads: list[ReadRecord] = []
        self._staged_outputs: dict[str, bytes] = {}
        self.pause_count = 0

    @property
    def step(self) -> str:
        return self._spec.name

    @property
    def attempt(self) -> int:
        return self._attempt

    @property
    def reads(self) -> tuple[ReadRecord, ...]:
        return tuple(self._reads)

    @property
    def staged_outputs(self) -> dict[str, bytes]:
        return dict(self._staged_outputs)

    def normalize(self, path: str) -> str:
        return self._coordinator.normalize_path(path)

    def exists(self, path: str) -> bool:
        """Return whether a path exists without recording it as a read."""

        return self._coordinator.current_stamp(path).exists

    def read(self, path: str) -> bytes:
        """Read a file and record its path plus observed version."""

        key = self._coordinator.normalize_path(path)
        data, stamp = self._coordinator.read_bytes(key)
        self._reads.append(ReadRecord(path=key, version=stamp, sequence=len(self._reads) + 1))
        return data

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self.read(path).decode(encoding)

    def read_version(self, path: str) -> VersionStamp:
        """Record and return only a file's current version.

        This is useful for lightweight synthetic actions.  It is still an
        actual coordinator observation, not an unverified user-supplied edge.
        """

        key = self._coordinator.normalize_path(path)
        stamp = self._coordinator.current_stamp(key)
        self._reads.append(ReadRecord(path=key, version=stamp, sequence=len(self._reads) + 1))
        return stamp

    def write_output(self, path: str, data: bytes | str) -> None:
        """Stage a declared output.

        Staged bytes stay in the context until the coordinator has validated
        all read versions and dependency-graph effects.  A stale or cyclic run
        therefore cannot replace a published artifact.
        """

        key = self._coordinator.normalize_path(path)
        if key not in self._spec.outputs:
            raise UndeclaredOutputError(
                f"step {self._spec.name!r} staged undeclared output {key!r}; "
                f"declared outputs: {sorted(self._spec.outputs)}"
            )
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._staged_outputs[key] = bytes(data)

    def pause(self, reason: str = "checkpoint") -> Any:
        """Invoke the run's pause hook, if one was supplied.

        A hook may call :meth:`PauseEvent.run_step` to interleave a producer.
        """

        self.pause_count += 1
        if self._pause_hook is None:
            return None
        event = PauseEvent(
            coordinator=self._coordinator,
            session=self._session,
            step=self._spec.name,
            attempt=self._attempt,
            reason=reason,
            context=self,
        )
        return self._pause_hook(event)
