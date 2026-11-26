"""Exceptions raised by the version-aware buffer scheduler."""

from __future__ import annotations

from typing import Any


class SchedulerError(Exception):
    """Base class for all scheduler errors."""


class ValidationError(SchedulerError, ValueError):
    """A request or model layout is invalid."""


class DuplicateRequestError(SchedulerError, ValueError):
    """A request id was already submitted."""


class UnknownRequestError(SchedulerError, KeyError):
    """Reference to an unknown request id."""

    def __init__(self, request_id: str):
        super().__init__(f"unknown request: {request_id!r}")
        self.request_id = request_id


class UnknownBatchError(SchedulerError, KeyError):
    """Reference to an unknown batch id."""

    def __init__(self, batch_id: str):
        super().__init__(f"unknown batch: {batch_id!r}")
        self.batch_id = batch_id


class UnknownBufferError(SchedulerError, KeyError):
    """Reference to an unknown stable buffer id."""

    def __init__(self, buffer_id: str):
        super().__init__(f"unknown buffer: {buffer_id!r}")
        self.buffer_id = buffer_id


class BufferUnavailableError(SchedulerError, RuntimeError):
    """A known buffer was evicted or permanently released."""


class UnknownModelError(SchedulerError, KeyError):
    """Reference to an unregistered model version."""

    def __init__(self, version: Any):
        super().__init__(f"unknown model version: {version!r}")
        self.version = version


class ModelVersionConflictError(SchedulerError, ValueError):
    """An existing model version was redefined with a different layout."""


class DependencyError(SchedulerError, ValueError):
    """Dependencies are missing, self-referential, or cyclic."""


class FormatConflictError(SchedulerError, ValueError):
    """No zero-copy format/layout is acceptable to all parties."""


class ShapeMismatchError(SchedulerError, ValueError):
    """A view or overwrite does not fit the shared region."""


class InvalidStateError(SchedulerError, RuntimeError):
    """An operation is invalid for the current request/buffer state."""


class BufferBusyError(SchedulerError, RuntimeError):
    """A region cannot be force-released while leased."""


class MemoryPressureError(SchedulerError, MemoryError):
    """The simulated pool cannot satisfy an allocation after reclamation."""

    def __init__(
        self,
        message: str,
        *,
        needed_bytes: int | None = None,
        capacity: int | None = None,
        free_bytes: int | None = None,
        steps: list[Any] | None = None,
        result: Any | None = None,
    ):
        super().__init__(message)
        self.needed_bytes = needed_bytes
        self.capacity = capacity
        self.free_bytes = free_bytes
        self.steps = steps or []
        self.result = result
