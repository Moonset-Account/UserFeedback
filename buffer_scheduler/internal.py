"""Mutable internal scheduler entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import (
    AccessMode,
    LayoutBinding,
    ModelLayout,
    RegionInfo,
    RequestState,
    StageRequest,
)


@dataclass
class Region:
    region_id: str
    buffer_id: str
    generation: int
    offset: int
    size: int
    alignment: int
    layout: LayoutBinding
    movable: bool = True
    evictable: bool = False
    active: bool = True
    payload: Any = None

    def info(self) -> RegionInfo:
        return RegionInfo(
            region_id=self.region_id,
            buffer_id=self.buffer_id,
            generation=self.generation,
            offset=self.offset,
            size=self.size,
            alignment=self.alignment,
            active=self.active,
            model_version=self.layout.model_version,
            layout_key=self.layout.layout_key,
            format=self.layout.format,
            movable=self.movable,
            evictable=self.evictable,
        )


@dataclass
class Lease:
    request_id: str
    mode: AccessMode
    region_id: str
    sequence: int


@dataclass
class Buffer:
    buffer_id: str
    batch_id: str
    producer_id: str
    layout: ModelLayout
    binding: LayoutBinding
    region: Region | None = None
    movable: bool = True
    evictable: bool = False
    leases: dict[str, Lease] = field(default_factory=dict)
    writer_id: str | None = None
    waiting_writers: list[str] = field(default_factory=list)
    alive: bool = True
    released: bool = False
    last_used_sequence: int = 0
    history: list[RegionInfo] = field(default_factory=list)

    def reader_count(self) -> int:
        return sum(1 for lease in self.leases.values() if lease.mode == AccessMode.READ)

    def info(self) -> RegionInfo | None:
        return self.region.info() if self.region is not None else None


@dataclass
class RequestRecord:
    spec: StageRequest
    batch_id: str
    created_sequence: int
    state: RequestState = RequestState.PENDING
    buffer_id: str | None = None
    binding: LayoutBinding | None = None
    blocked_reason: str | None = None
    terminal_reason: str | None = None
    submitted_trace_index: int = -1
    terminal_sequence: int | None = None
    wait_signature: str | None = None
    receipt: Any = None
    last_region: Any = None
    pressure_error: BaseException | None = None

    @property
    def request_id(self) -> str:
        return self.spec.request_id

    @property
    def access(self) -> AccessMode:
        return self.spec.access


@dataclass
class Batch:
    batch_id: str
    layout: ModelLayout
    auto_release: bool = True
    sealed: bool = False
    completed: bool = False
    released: bool = False
    request_ids: list[str] = field(default_factory=list)
    buffer_ids: list[str] = field(default_factory=list)
