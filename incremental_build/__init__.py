"""Incremental build coordination for late-reported generated-file reads."""

from .context import BuildContext, PauseEvent, PauseGate
from .coordinator import BuildCoordinator
from .errors import (
    BuildFailed,
    BuildCoordinationError,
    DependencyCycleError,
    DuplicateProducerError,
    StaleReadError,
    UndeclaredOutputError,
    UnknownOutputError,
    UnknownStepError,
)
from .models import AttemptRecord, BuildResult, ReadRecord, StepRun
from .versions import MISSING_VERSION, VersionStamp

IncrementalBuildCoordinator = BuildCoordinator

__all__ = [
    "AttemptRecord",
    "BuildContext",
    "BuildCoordinationError",
    "BuildCoordinator",
    "BuildFailed",
    "BuildResult",
    "DependencyCycleError",
    "DuplicateProducerError",
    "IncrementalBuildCoordinator",
    "MISSING_VERSION",
    "PauseEvent",
    "PauseGate",
    "ReadRecord",
    "StaleReadError",
    "StepRun",
    "UndeclaredOutputError",
    "UnknownOutputError",
    "UnknownStepError",
    "VersionStamp",
]
