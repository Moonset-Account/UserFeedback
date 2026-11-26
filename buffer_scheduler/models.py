"""Public value objects and reports for the buffer scheduler."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from .errors import FormatConflictError, ValidationError


class AccessMode(str, Enum):
    """How a scheduled stage intends to touch a buffer."""

    READ = "read"
    WRITE = "write"


class RequestState(str, Enum):
    """Lifecycle state of one submitted stage/branch."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ReleaseReason(str, Enum):
    """Stable reasons attached to lease and region releases."""

    READER_COMPLETED = "reader_completed"
    BRANCH_COMPLETED = "branch_completed"
    WRITER_COMPLETED = "writer_completed"
    REQUEST_COMPLETED = "request_completed"
    BRANCH_CANCELLED = "branch_cancelled"
    REQUEST_CANCELLED = "request_cancelled"
    DEPENDENCY_CANCELLED = "dependency_cancelled"
    BATCH_COMPLETED = "batch_completed"
    CALLER_RELEASED = "caller_released"
    MEMORY_PRESSURE_EVICTION = "memory_pressure_eviction"
    MEMORY_PRESSURE_RELOCATION = "memory_pressure_relocation"


FORMAT_ALIASES = {
    "float32": "fp32",
    "f32": "fp32",
    "float": "fp32",
    "float16": "fp16",
    "f16": "fp16",
    "half": "fp16",
    "bfloat16": "bf16",
    "bf16": "bf16",
    "int8": "int8",
    "uint8": "uint8",
    "int32": "int32",
    "i32": "int32",
    "int64": "int64",
    "i64": "int64",
}

DEFAULT_FORMAT_BYTES = {
    "fp32": 4,
    "fp16": 2,
    "bf16": 2,
    "int8": 1,
    "uint8": 1,
    "int32": 4,
    "int64": 8,
}


def normalize_format(value: str) -> str:
    """Normalize common tensor dtype spellings to ``fp32``-style names."""

    if not isinstance(value, str) or not value.strip():
        raise ValidationError("format must be a non-empty string")
    key = value.strip().lower().replace("-", "").replace("_", "")
    # Preserve names such as fp32 after removing separators.
    key = {"fp32": "fp32", "fp16": "fp16", "bf16": "bf16"}.get(key, key)
    return FORMAT_ALIASES.get(key, key)


def coerce_shape(value: "Shape | int | Sequence[int]") -> "Shape":
    if isinstance(value, Shape):
        return value
    if isinstance(value, int):
        return Shape((value,))
    return Shape(tuple(value))


@dataclass(frozen=True)
class Shape:
    """A dense tensor shape. An empty tuple denotes a scalar."""

    dims: tuple[int, ...]

    def __post_init__(self) -> None:
        dims = tuple(self.dims)
        for dim in dims:
            if isinstance(dim, bool) or not isinstance(dim, int):
                raise ValidationError(f"shape dimensions must be integers, got {dim!r}")
            if dim <= 0:
                raise ValidationError(f"shape dimensions must be positive, got {dim}")
        object.__setattr__(self, "dims", dims)

    @property
    def num_elements(self) -> int:
        count = 1
        for dim in self.dims:
            count *= dim
        return count

    @property
    def strides(self) -> tuple[int, ...]:
        out = [1] * len(self.dims)
        stride = 1
        for index in range(len(self.dims) - 1, -1, -1):
            out[index] = stride
            stride *= self.dims[index]
        return tuple(out)

    def as_tuple(self) -> tuple[int, ...]:
        return self.dims


@dataclass(frozen=True)
class LayoutBinding:
    """Immutable memory layout selected for one allocation/batch version."""

    model_version: str
    format: str
    itemsize: int
    shape: Shape
    strides: tuple[int, ...]
    logical_bytes: int
    alignment: int
    aligned_size: int
    layout_name: str = "dense-row-major"

    @property
    def layout_key(self) -> str:
        return (
            f"{self.model_version}:{self.layout_name}:{self.format}:"
            f"a{self.alignment}:s{self.aligned_size}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "format": self.format,
            "itemsize": self.itemsize,
            "shape": list(self.shape.dims),
            "strides": list(self.strides),
            "logical_bytes": self.logical_bytes,
            "alignment": self.alignment,
            "aligned_size": self.aligned_size,
            "layout_name": self.layout_name,
            "layout_key": self.layout_key,
        }


@dataclass(frozen=True)
class ModelLayout:
    """Model-version-specific allocation rules.

    A batch receives its own ``ModelLayout`` instance at submission time.
    Hot-swapping the active layout therefore cannot mutate old batches.
    """

    version: str
    alignment: int = 256
    format_widths: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_FORMAT_BYTES))
    supported_formats: frozenset[str] | None = None
    layout_name: str = "dense-row-major"

    def __post_init__(self) -> None:
        version = str(self.version)
        if not version:
            raise ValidationError("model version must be non-empty")
        if not isinstance(self.alignment, int) or self.alignment <= 0:
            raise ValidationError("alignment must be a positive integer")
        widths = {
            normalize_format(k): int(v)
            for k, v in dict(self.format_widths).items()
            if int(v) > 0
        }
        if not widths:
            raise ValidationError("format_widths must contain at least one positive width")
        supported = (
            frozenset(normalize_format(f) for f in self.supported_formats)
            if self.supported_formats is not None
            else frozenset(widths)
        )
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "format_widths", MappingProxyType(widths))
        object.__setattr__(self, "supported_formats", supported)

    def itemsize(self, fmt: str) -> int:
        normalized = normalize_format(fmt)
        try:
            return int(self.format_widths[normalized])  # type: ignore[index]
        except KeyError as exc:
            raise FormatConflictError(f"model {self.version!r} has no width for {fmt!r}") from exc

    def bind(
        self,
        shape: Shape | Sequence[int],
        available_formats: Sequence[str],
        *,
        policy: str = "ordered",
    ) -> LayoutBinding:
        tensor_shape = coerce_shape(shape)
        requested = [normalize_format(f) for f in available_formats]
        if not requested:
            raise ValidationError("available_formats must not be empty")
        acceptable = [f for f in requested if f in self.supported_formats]  # type: ignore[operator]
        if not acceptable:
            raise FormatConflictError(
                f"none of {list(available_formats)!r} is supported by model {self.version!r} "
                f"formats={sorted(self.supported_formats)!r}"
            )
        if policy == "smallest":
            selected = min(acceptable, key=lambda f: self.itemsize(f))
        elif policy in {"ordered", "preference"}:
            selected = acceptable[0]
        else:
            raise ValidationError(f"unknown format selection policy: {policy!r}")
        itemsize = self.itemsize(selected)
        logical = tensor_shape.num_elements * itemsize
        aligned = ((logical + self.alignment - 1) // self.alignment) * self.alignment
        return LayoutBinding(
            model_version=self.version,
            format=selected,
            itemsize=itemsize,
            shape=tensor_shape,
            strides=tensor_shape.strides,
            logical_bytes=logical,
            alignment=self.alignment,
            aligned_size=max(aligned, self.alignment),
            layout_name=self.layout_name,
        )


@dataclass(frozen=True)
class StageRequest:
    """A caller submission for one producer, branch reader, or overwrite."""

    request_id: str
    shape: Shape | Sequence[int]
    available_formats: Sequence[str]
    batch_id: str = "default"
    depends_on: Sequence[str] = field(default_factory=tuple)
    access: AccessMode | str = AccessMode.WRITE
    source_id: str | None = None
    buffer_id: str | None = None
    stage_name: str | None = None
    model_version: str | int | None = None
    on_complete: Callable[[CompletionReceipt], None] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    evictable: bool = False
    movable: bool = True
    receipt_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValidationError("request_id must be a non-empty string")
        if not isinstance(self.batch_id, str) or not self.batch_id.strip():
            raise ValidationError("batch_id must be a non-empty string")
        shape = coerce_shape(self.shape)
        formats = tuple(normalize_format(f) for f in self.available_formats)
        if not formats:
            raise ValidationError("available_formats must not be empty")
        deps = tuple(str(d) for d in self.depends_on)
        access = AccessMode(self.access)
        if access == AccessMode.READ and not self.source_id:
            raise ValidationError("a read request requires source_id")
        metadata = MappingProxyType(dict(self.metadata))
        object.__setattr__(self, "shape", shape)
        object.__setattr__(self, "available_formats", formats)
        object.__setattr__(self, "depends_on", deps)
        object.__setattr__(self, "access", access)
        object.__setattr__(self, "metadata", metadata)
        if self.model_version is not None:
            object.__setattr__(self, "model_version", str(self.model_version))

    @classmethod
    def branch(
        cls,
        branch_id: str,
        source_id: str,
        shape: Shape | Sequence[int],
        available_formats: Sequence[str] = ("fp32",),
        **kwargs: Any,
    ) -> "StageRequest":
        """Convenience constructor for a zero-copy read-only branch."""

        depends_on = tuple(kwargs.pop("depends_on", ()))
        if source_id not in depends_on:
            depends_on = (source_id,) + tuple(depends_on)
        return cls(
            request_id=branch_id,
            shape=shape,
            available_formats=available_formats,
            depends_on=depends_on,
            access=AccessMode.READ,
            source_id=source_id,
            stage_name=kwargs.pop("stage_name", "branch"),
            **kwargs,
        )


@dataclass(frozen=True)
class RegionInfo:
    """Immutable snapshot of an actual placed memory region."""

    region_id: str
    buffer_id: str
    generation: int
    offset: int
    size: int
    alignment: int
    active: bool
    model_version: str
    layout_key: str
    format: str
    movable: bool
    evictable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "region_id": self.region_id,
            "buffer_id": self.buffer_id,
            "generation": self.generation,
            "offset": self.offset,
            "size": self.size,
            "alignment": self.alignment,
            "active": self.active,
            "model_version": self.model_version,
            "layout_key": self.layout_key,
            "format": self.format,
            "movable": self.movable,
            "evictable": self.evictable,
        }


@dataclass(frozen=True)
class ReleaseRecord:
    """One lease or physical region release and its explicit reason."""

    sequence: int
    release_kind: str  # "lease" or "region"
    region: RegionInfo
    reason: str
    request_id: str | None = None
    buffer_id: str | None = None
    replacement_region_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "release_kind": self.release_kind,
            "reason": self.reason,
            "request_id": self.request_id,
            "buffer_id": self.buffer_id,
            "replacement_region_id": self.replacement_region_id,
            "region": self.region.to_dict(),
        }


@dataclass(frozen=True)
class StepReport:
    """One observable scheduling action.

    ``region`` is the actual region used, shared, granted, relocated, or
    released by this step.  ``release`` is present exactly for a release event.
    """

    sequence: int
    action: str
    batch_id: str | None = None
    request_id: str | None = None
    buffer_id: str | None = None
    region: RegionInfo | None = None
    granted: bool | None = None
    release: ReleaseRecord | None = None
    model_version: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def release_reason(self) -> str | None:
        return self.release.reason if self.release else None

    @property
    def actual_region(self) -> RegionInfo | None:
        return self.region

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "action": self.action,
            "batch_id": self.batch_id,
            "request_id": self.request_id,
            "buffer_id": self.buffer_id,
            "region": self.region.to_dict() if self.region else None,
            "granted": self.granted,
            "release": self.release.to_dict() if self.release else None,
            "release_reason": self.release_reason,
            "model_version": self.model_version,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class CompletionReceipt:
    """Completion/cancellation receipt returned and optionally delivered."""

    receipt_id: str
    request_id: str
    batch_id: str
    status: RequestState
    region: RegionInfo | None
    model_version: str | None
    layout_key: str | None
    selected_format: str | None
    submitted_sequence: int
    completed_sequence: int
    depends_on: tuple[str, ...]
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "request_id": self.request_id,
            "batch_id": self.batch_id,
            "status": self.status.value,
            "region": self.region.to_dict() if self.region else None,
            "model_version": self.model_version,
            "layout_key": self.layout_key,
            "selected_format": self.selected_format,
            "submitted_sequence": self.submitted_sequence,
            "completed_sequence": self.completed_sequence,
            "depends_on": list(self.depends_on),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RequestInfo:
    """Immutable scheduler state snapshot for one request."""

    request_id: str
    batch_id: str
    state: RequestState
    access: AccessMode
    depends_on: tuple[str, ...]
    source_id: str | None
    buffer_id: str | None
    region: RegionInfo | None
    model_version: str | None
    layout_key: str | None
    selected_format: str | None
    blocked_reason: str | None
    terminal_reason: str | None
    active_lease: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "batch_id": self.batch_id,
            "state": self.state.value,
            "access": self.access.value,
            "depends_on": list(self.depends_on),
            "source_id": self.source_id,
            "buffer_id": self.buffer_id,
            "region": self.region.to_dict() if self.region else None,
            "model_version": self.model_version,
            "layout_key": self.layout_key,
            "selected_format": self.selected_format,
            "blocked_reason": self.blocked_reason,
            "terminal_reason": self.terminal_reason,
            "active_lease": self.active_lease,
        }


@dataclass
class SchedulerResult:
    """Result of a submission, completion, cancellation, or maintenance call."""

    operation: str
    ok: bool = True
    request_id: str | None = None
    batch_id: str | None = None
    state: RequestState | None = None
    steps: list[StepReport] = field(default_factory=list)
    receipt: CompletionReceipt | None = None
    receipts: list[CompletionReceipt] = field(default_factory=list)
    request: RequestInfo | None = None
    error: BaseException | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def region(self) -> RegionInfo | None:
        for step in reversed(self.steps):
            if step.region is not None:
                return step.region
        return self.request.region if self.request else None

    @property
    def regions(self) -> list[RegionInfo]:
        out: list[RegionInfo] = []
        for step in self.steps:
            if step.region is not None:
                out.append(step.region)
        return out

    @property
    def releases(self) -> list[ReleaseRecord]:
        return [step.release for step in self.steps if step.release is not None]

    @property
    def release_reasons(self) -> list[str]:
        return [r.reason for r in self.releases]

    @property
    def changed(self) -> bool:
        return bool(self.data.get("changed", True))

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "ok": self.ok,
            "request_id": self.request_id,
            "batch_id": self.batch_id,
            "state": self.state.value if self.state else None,
            "steps": [s.to_dict() for s in self.steps],
            "receipt": self.receipt.to_dict() if self.receipt else None,
            "receipts": [r.to_dict() for r in self.receipts],
            "request": self.request.to_dict() if self.request else None,
            "error": str(self.error) if self.error else None,
            "data": dict(self.data),
        }
