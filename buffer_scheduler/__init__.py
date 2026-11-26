"""Version-aware zero-copy buffer scheduling for staged inference."""

from .errors import (
    BufferBusyError,
    BufferUnavailableError,
    DependencyError,
    DuplicateRequestError,
    FormatConflictError,
    InvalidStateError,
    MemoryPressureError,
    ModelVersionConflictError,
    SchedulerError,
    ShapeMismatchError,
    UnknownBatchError,
    UnknownBufferError,
    UnknownModelError,
    UnknownRequestError,
    ValidationError,
)
from .models import (
    AccessMode,
    CompletionReceipt,
    LayoutBinding,
    ModelLayout,
    RegionInfo,
    ReleaseReason,
    ReleaseRecord,
    RequestInfo,
    RequestState,
    SchedulerResult,
    Shape,
    StageRequest,
    StepReport,
)
from .scheduler import BufferScheduler

ModelVersionBufferScheduler = BufferScheduler
ModelVersionAwareBufferScheduler = BufferScheduler
VersionAwareBufferScheduler = BufferScheduler

__all__ = [
    "AccessMode",
    "BufferBusyError",
    "BufferScheduler",
    "BufferUnavailableError",
    "CompletionReceipt",
    "DependencyError",
    "DuplicateRequestError",
    "FormatConflictError",
    "InvalidStateError",
    "LayoutBinding",
    "MemoryPressureError",
    "ModelLayout",
    "ModelVersionAwareBufferScheduler",
    "ModelVersionBufferScheduler",
    "ModelVersionConflictError",
    "VersionAwareBufferScheduler",
    "RegionInfo",
    "ReleaseReason",
    "ReleaseRecord",
    "RequestInfo",
    "RequestState",
    "SchedulerError",
    "SchedulerResult",
    "Shape",
    "ShapeMismatchError",
    "StageRequest",
    "StepReport",
    "UnknownBatchError",
    "UnknownBufferError",
    "UnknownModelError",
    "UnknownRequestError",
    "ValidationError",
]
