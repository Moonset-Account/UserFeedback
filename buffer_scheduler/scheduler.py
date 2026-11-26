"""Model-version-aware, zero-copy buffer scheduling for simulated GPU stages."""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any, Callable, Mapping, Sequence

from .errors import (
    BufferBusyError,
    BufferUnavailableError,
    DependencyError,
    DuplicateRequestError,
    InvalidStateError,
    MemoryPressureError,
    UnknownBatchError,
    UnknownBufferError,
    UnknownModelError,
    UnknownRequestError,
    ValidationError,
)
from .internal import Batch, Buffer, Region, RequestRecord
from .models import (
    AccessMode,
    CompletionReceipt,
    ModelLayout,
    ReleaseReason,
    ReleaseRecord,
    RequestInfo,
    RequestState,
    SchedulerResult,
    StageRequest,
    StepReport,
)
from .pool import MemoryPool

_UNSET = object()


class BufferScheduler:
    """Schedule shaped tensors over a versioned simulated buffer pool.

    The scheduler is deterministic and thread-safe.  Every state change is
    appended to :attr:`trace` as a :class:`StepReport`.  Public methods also
    return only the steps produced by that call.
    """

    def __init__(
        self,
        memory_limit: int = 64 * 1024 * 1024,
        *,
        model: ModelLayout | None = None,
        models: Sequence[ModelLayout] | Mapping[str, ModelLayout] | None = None,
        active_version: str | int | None = None,
        format_policy: str = "ordered",
        fair_writer_queue: bool = True,
        raise_on_pressure: bool = False,
    ) -> None:
        if format_policy not in {"ordered", "preference", "smallest"}:
            raise ValidationError(f"unknown format_policy: {format_policy!r}")
        self.pool = MemoryPool(memory_limit)
        self.memory_limit = int(memory_limit)
        self.format_policy = format_policy
        self.fair_writer_queue = fair_writer_queue
        self.raise_on_pressure = raise_on_pressure

        self._models: dict[str, ModelLayout] = {}
        initial_models: list[ModelLayout] = []
        if model is not None:
            initial_models.append(model)
        if isinstance(models, Mapping):
            initial_models.extend(models.values())
        elif models is not None:
            initial_models.extend(models)
        for item in initial_models:
            if not isinstance(item, ModelLayout):
                raise ValidationError("models must contain ModelLayout instances")
            self._models[item.version] = item

        if active_version is not None:
            self._active_version = str(active_version)
            if self._active_version not in self._models:
                raise UnknownModelError(active_version)
        elif model is not None:
            self._active_version = model.version
        elif self._models:
            self._active_version = next(iter(self._models))
        else:
            default = ModelLayout(version="default", alignment=256)
            self._models[default.version] = default
            self._active_version = default.version

        self._batches: dict[str, Batch] = {}
        self._requests: dict[str, RequestRecord] = {}
        self._buffers: dict[str, Buffer] = {}
        self.receipts: dict[str, CompletionReceipt] = {}
        self.trace: list[StepReport] = []
        self.callback_errors: list[BaseException] = []

        self._sequence = 0
        self._order = 0
        self._region_count = 0
        self._callbacks: list[tuple[Callable[[CompletionReceipt], None], CompletionReceipt]] = []
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ basic

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _next_order(self) -> int:
        self._order += 1
        return self._order

    def _next_region_id(self) -> str:
        self._region_count += 1
        return f"R{self._region_count:04d}"

    def _emit(
        self,
        action: str,
        *,
        batch_id: str | None = None,
        request_id: str | None = None,
        buffer_id: str | None = None,
        region: Any = None,
        granted: bool | None = None,
        release: ReleaseRecord | None = None,
        model_version: str | None = None,
        **detail: Any,
    ) -> StepReport:
        step = StepReport(
            sequence=self._next_sequence(),
            action=action,
            batch_id=batch_id,
            request_id=request_id,
            buffer_id=buffer_id,
            region=region,
            granted=granted,
            release=release,
            model_version=model_version or (region.model_version if region else None),
            detail=dict(detail),
        )
        self.trace.append(step)
        if request_id is not None:
            req = self._requests.get(request_id)
            if req is not None and region is not None:
                req.last_region = region
        return step

    def _queue_callback(self, receipt: CompletionReceipt, callback: Callable[..., None] | None) -> None:
        if callback is not None:
            self._callbacks.append((callback, receipt))

    def _flush_callbacks(
        self,
        queued: list[tuple[Callable[[CompletionReceipt], None], CompletionReceipt]],
    ) -> None:
        for callback, receipt in queued:
            try:
                callback(receipt)
            except BaseException as exc:  # defensive: one bad receipt must not corrupt scheduling
                self.callback_errors.append(exc)

    def _result(
        self,
        operation: str,
        before: int,
        *,
        req: RequestRecord | None = None,
        ok: bool = True,
        receipt: CompletionReceipt | None = None,
        receipts: list[CompletionReceipt] | None = None,
        error: BaseException | None = None,
        data: dict[str, Any] | None = None,
    ) -> SchedulerResult:
        steps = list(self.trace[before:])
        request_info = self._info_locked(req) if req is not None else None
        primary_receipt = receipt or (req.receipt if req is not None else None)
        return SchedulerResult(
            operation=operation,
            ok=ok,
            request_id=req.request_id if req is not None else None,
            batch_id=req.batch_id if req is not None else (data or {}).get("batch_id"),
            state=req.state if req is not None else None,
            steps=steps,
            receipt=primary_receipt,
            receipts=receipts or ([primary_receipt] if primary_receipt is not None else []),
            request=request_info,
            error=error or (req.pressure_error if req is not None else None),
            data=data or {},
        )

    # ----------------------------------------------------------------- models

    @property
    def active_model_version(self) -> str:
        return self._active_version

    def get_model(self, version: str | int | None = None) -> ModelLayout:
        key = self._active_version if version is None else str(version)
        try:
            return self._models[key]
        except KeyError as exc:
            raise UnknownModelError(version) from exc

    def list_models(self) -> dict[str, ModelLayout]:
        with self._lock:
            return dict(self._models)

    def register_model(self, layout: ModelLayout, *, activate: bool | None = None) -> SchedulerResult:
        with self._lock:
            before = len(self.trace)
            existing = self._models.get(layout.version)
            if existing is not None and existing != layout:
                from .errors import ModelVersionConflictError

                raise ModelVersionConflictError(
                    f"model version {layout.version!r} is already registered with a different layout"
                )
            self._models[layout.version] = layout
            should_activate = self._active_version is None if activate is None else activate
            previous = self._active_version
            if should_activate:
                self._active_version = layout.version
            self._emit(
                "model_registered",
                model_version=layout.version,
                activated=should_activate,
                previous_version=previous,
                alignment=layout.alignment,
                layout_name=layout.layout_name,
            )
            queued = self._callbacks
            self._callbacks = []
            result = self._result(
                "register_model",
                before,
                data={
                    "active_version": self._active_version,
                    "previous_version": previous,
                    "changed": True,
                },
            )
        self._flush_callbacks(queued)
        return result

    def hot_swap_model(self, layout: ModelLayout) -> SchedulerResult:
        """Activate a new layout without mutating pinned batches."""

        with self._lock:
            before = len(self.trace)
            previous = self._active_version
            existing = self._models.get(layout.version)
            if existing is not None and existing != layout:
                from .errors import ModelVersionConflictError

                raise ModelVersionConflictError(
                    f"model version {layout.version!r} already has a different layout"
                )
            self._models[layout.version] = layout
            self._active_version = layout.version
            self._emit(
                "model_hot_swapped",
                model_version=layout.version,
                previous_version=previous,
                alignment=layout.alignment,
                layout_name=layout.layout_name,
            )
            queued = self._callbacks
            self._callbacks = []
            result = self._result(
                "hot_swap_model",
                before,
                data={
                    "active_version": layout.version,
                    "previous_version": previous,
                    "changed": previous != layout.version,
                },
            )
        self._flush_callbacks(queued)
        return result

    def activate_model(self, version: str | int) -> SchedulerResult:
        key = str(version)
        with self._lock:
            before = len(self.trace)
            if key not in self._models:
                raise UnknownModelError(version)
            previous = self._active_version
            self._active_version = key
            self._emit("model_activated", model_version=key, previous_version=previous)
            result = self._result(
                "activate_model",
                before,
                data={"active_version": key, "previous_version": previous},
            )
        return result

    # ------------------------------------------------------------- batches/IO

    def _create_batch_locked(
        self,
        batch_id: str,
        model_version: str | int | None = None,
        *,
        auto_release: bool = True,
    ) -> Batch:
        if batch_id in self._batches:
            return self._batches[batch_id]
        layout = self.get_model(model_version)
        batch = Batch(
            batch_id=batch_id,
            layout=layout,
            auto_release=auto_release,
        )
        self._batches[batch_id] = batch
        self._emit(
            "batch_opened",
            batch_id=batch_id,
            model_version=layout.version,
            alignment=layout.alignment,
            layout_name=layout.layout_name,
            auto_release=auto_release,
        )
        return batch

    @staticmethod
    def _coerce_stage(
        request: StageRequest | Mapping[str, Any] | None,
        kwargs: dict[str, Any],
    ) -> StageRequest:
        if request is None:
            if not kwargs:
                raise ValidationError("a StageRequest or keyword arguments are required")
            return StageRequest(**kwargs)
        if kwargs:
            if isinstance(request, StageRequest):
                return replace(request, **kwargs)
            data = dict(request)
            data.update(kwargs)
            return StageRequest(**data)
        if isinstance(request, StageRequest):
            return request
        if isinstance(request, Mapping):
            return StageRequest(**dict(request))
        raise ValidationError("request must be a StageRequest, mapping, or keyword arguments")

    def submit_stage(
        self,
        request: StageRequest | Mapping[str, Any] | None = None,
        /,
        *,
        strict: bool | None = None,
        **kwargs: Any,
    ) -> SchedulerResult:
        """Submit one producer, reader branch, or overwrite stage."""

        spec = self._coerce_stage(request, kwargs)
        pressure_raise = self.raise_on_pressure if strict is None else strict
        pending_raise: MemoryPressureError | None = None
        with self._lock:
            before = len(self.trace)
            req = self._submit_locked(spec)
            self._pump_pending_locked()
            self._finalize_batches_locked()
            ok = req.state != RequestState.PENDING or req.blocked_reason != "memory_pressure"
            result = self._result("submit_stage", before, req=req, ok=ok)
            queued = self._callbacks
            self._callbacks = []
            if not ok and pressure_raise and req.pressure_error is not None:
                err = req.pressure_error
                pending_raise = MemoryPressureError(
                    str(err),
                    needed_bytes=getattr(err, "needed_bytes", None),
                    capacity=self.memory_limit,
                    free_bytes=self.pool.free_bytes,
                    steps=result.steps,
                    result=result,
                )
        self._flush_callbacks(queued)
        if pending_raise is not None:
            raise pending_raise
        return result

    submit = submit_stage

    def _submit_locked(self, spec: StageRequest) -> RequestRecord:
        if spec.request_id in self._requests:
            raise DuplicateRequestError(f"duplicate request_id: {spec.request_id!r}")
        if spec.request_id in spec.depends_on:
            raise DependencyError(f"request {spec.request_id!r} cannot depend on itself")

        batch = self._create_batch_locked(spec.batch_id, spec.model_version)
        if spec.model_version is not None and spec.model_version != batch.layout.version:
            raise ValidationError(
                f"request {spec.request_id!r} asks for model {spec.model_version!r}, "
                f"but batch {batch.batch_id!r} is pinned to {batch.layout.version!r}"
            )

        created = self._next_order()
        req = RequestRecord(spec=spec, batch_id=batch.batch_id, created_sequence=created)
        self._requests[spec.request_id] = req
        batch.request_ids.append(spec.request_id)
        self._emit(
            "submitted",
            batch_id=batch.batch_id,
            request_id=spec.request_id,
            model_version=batch.layout.version,
            access=spec.access.value,
            source_id=spec.source_id,
            depends_on=list(spec.depends_on),
            shape=list(spec.shape.dims),
            available_formats=list(spec.available_formats),
        )
        self._dispatch_locked(req)
        return req

    def submit_batch(
        self,
        batch_id: str,
        stages: Sequence[StageRequest | Mapping[str, Any]] | None = None,
        *,
        model_version: str | int | None = None,
        auto_release: bool = True,
        strict: bool = False,
    ) -> SchedulerResult:
        """Open a version-pinned batch, add all stages, and seal its DAG."""

        with self._lock:
            before = len(self.trace)
            if batch_id in self._batches:
                raise ValidationError(f"batch already exists: {batch_id!r}")
            batch = self._create_batch_locked(
                batch_id, model_version, auto_release=auto_release
            )
            normalized: list[StageRequest] = []
            for item in stages or ():
                spec = item if isinstance(item, StageRequest) else StageRequest(**dict(item))
                if spec.batch_id != batch_id:
                    spec = replace(spec, batch_id=batch_id)
                if spec.model_version is not None and spec.model_version != batch.layout.version:
                    raise ValidationError("stage model_version conflicts with submit_batch version")
                normalized.append(spec)

            pressure_error: MemoryPressureError | None = None
            for spec in normalized:
                req = self._submit_locked(spec)
                if req.pressure_error is not None and pressure_error is None:
                    pressure_error = req.pressure_error

            batch.sealed = True
            self._validate_dependency_graph_locked(batch_id)
            self._pump_pending_locked()
            self._finalize_batches_locked()
            result = self._result(
                "submit_batch",
                before,
                data={
                    "batch_id": batch_id,
                    "request_ids": list(batch.request_ids),
                    "sealed": True,
                    "active_version": self._active_version,
                },
            )
            result.ok = pressure_error is None
            queued = self._callbacks
            self._callbacks = []
        self._flush_callbacks(queued)
        if strict and pressure_error is not None:
            raise MemoryPressureError(
                str(pressure_error),
                steps=result.steps,
                result=result,
                capacity=self.memory_limit,
                free_bytes=self.pool.free_bytes,
            )
        return result

    def submit_output(
        self,
        request_id: str,
        shape: "Shape | Sequence[int]",
        available_formats: Sequence[str],
        dependent_stages: Sequence[str | Mapping[str, Any] | StageRequest] = (),
        *,
        batch_id: str | None = None,
        model_version: str | int | None = None,
        on_complete: Callable[[CompletionReceipt], None] | None = None,
        receipt_id: str | None = None,
        evictable: bool = False,
        movable: bool = True,
    ) -> SchedulerResult:
        """Submit one GPU output plus its downstream read-only branches.

        String entries in ``dependent_stages`` create branch request ids of the
        form ``"<request_id>:<stage_name>"``.
        """

        batch = batch_id or f"output-{request_id}"
        producer = StageRequest(
            request_id=request_id,
            shape=shape,
            available_formats=available_formats,
            batch_id=batch,
            model_version=model_version,
            on_complete=on_complete,
            receipt_id=receipt_id,
            evictable=evictable,
            movable=movable,
            stage_name="gpu-output",
        )
        stages: list[StageRequest] = [producer]
        branch_ids: list[str] = []

        for index, item in enumerate(dependent_stages):
            extra_deps: tuple[str, ...] = ()
            callback = None
            branch_receipt_id = None
            branch_evictable = False
            if isinstance(item, str):
                name = item
                branch_id = f"{request_id}:{name}"
                branch_shape = shape
                branch_formats = available_formats
            elif isinstance(item, StageRequest):
                stages.append(replace(item, batch_id=batch))
                branch_ids.append(item.request_id)
                continue
            elif isinstance(item, Mapping):
                name = str(item.get("stage_name") or item.get("name") or item.get("stage") or f"stage-{index}")
                branch_id = str(item.get("request_id") or item.get("branch_id") or f"{request_id}:{name}")
                branch_shape = item.get("shape", shape)
                branch_formats = item.get("available_formats", item.get("formats", available_formats))
                extra_deps = tuple(item.get("depends_on", ()))
                callback = item.get("on_complete")
                branch_receipt_id = item.get("receipt_id")
                branch_evictable = bool(item.get("evictable", False))
            else:
                raise ValidationError("dependent stage entries must be strings, mappings, or StageRequests")

            depends_on = (request_id,) + tuple(d for d in extra_deps if d != request_id)
            branch = StageRequest(
                request_id=branch_id,
                shape=branch_shape,
                available_formats=branch_formats,
                batch_id=batch,
                depends_on=depends_on,
                access=AccessMode.READ,
                source_id=request_id,
                stage_name=name,
                on_complete=callback,
                receipt_id=branch_receipt_id,
                evictable=branch_evictable,
            )
            stages.append(branch)
            branch_ids.append(branch_id)

        result = self.submit_batch(
            batch,
            stages,
            model_version=model_version,
            auto_release=True,
        )
        result.operation = "submit_output"
        result.data["dependent_stages"] = branch_ids
        result.data["batch_id"] = batch
        return result

    def seal_batch(self, batch_id: str) -> SchedulerResult:
        with self._lock:
            before = len(self.trace)
            batch = self._batches.get(batch_id)
            if batch is None:
                raise UnknownBatchError(batch_id)
            batch.sealed = True
            self._validate_dependency_graph_locked(batch_id)
            self._pump_pending_locked()
            self._finalize_batches_locked()
            result = self._result("seal_batch", before, data={"batch_id": batch_id})
            queued = self._callbacks
            self._callbacks = []
        self._flush_callbacks(queued)
        return result

    def _validate_dependency_graph_locked(self, batch_id: str | None = None) -> None:
        ids = set(self._requests)
        if batch_id is not None:
            batch = self._batches[batch_id]
            ids = set(batch.request_ids)
        for request_id in ids:
            req = self._requests[request_id]
            for dep in req.spec.depends_on:
                if dep not in self._requests:
                    raise DependencyError(
                        f"request {request_id!r} depends on unknown request/stage {dep!r}"
                    )
        # DFS cycle detection over the global known graph.
        state: dict[str, int] = {}

        def visit(node: str, stack: list[str]) -> None:
            if state.get(node) == 1:
                cycle = " -> ".join(stack + [node])
                raise DependencyError(f"dependency cycle detected: {cycle}")
            if state.get(node) == 2 or node not in self._requests:
                return
            state[node] = 1
            for dep in self._requests[node].spec.depends_on:
                visit(dep, stack + [node])
            state[node] = 2

        for request_id in list(self._requests):
            if batch_id is None or request_id in ids:
                visit(request_id, [])

    # ------------------------------------------------------------ scheduling

    def _pump_pending_locked(self) -> None:
        changed = True
        while changed:
            changed = False
            for req in sorted(self._requests.values(), key=lambda item: item.created_sequence):
                if req.state != RequestState.PENDING:
                    continue
                old_state = req.state
                try:
                    self._dispatch_locked(req)
                except Exception as exc:  # scheduling rejection becomes a failed receipt
                    self._fail_request_locked(req, exc)
                if req.state != old_state:
                    changed = True

    def _dispatch_locked(self, req: RequestRecord) -> None:
        if req.state != RequestState.PENDING:
            return

        missing: list[str] = []
        terminated_dep: RequestRecord | None = None
        for dep_id in req.spec.depends_on:
            dep = self._requests.get(dep_id)
            if dep is None:
                missing.append(dep_id)
            elif dep.state in {RequestState.CANCELLED, RequestState.FAILED}:
                terminated_dep = dep
                break
            elif dep.state != RequestState.COMPLETED:
                missing.append(dep_id)

        if terminated_dep is not None:
            if terminated_dep.state == RequestState.CANCELLED:
                reason = ReleaseReason.DEPENDENCY_CANCELLED.value
                state = RequestState.CANCELLED
            else:
                reason = "dependency_failed"
                state = RequestState.FAILED
            self._terminate_request_locked(req, state, reason)
            return

        if missing:
            signature = "dependency:" + ",".join(missing)
            if req.wait_signature != signature:
                req.blocked_reason = "waiting_dependency"
                req.wait_signature = signature
                self._emit(
                    "dependency_waiting",
                    batch_id=req.batch_id,
                    request_id=req.request_id,
                    granted=False,
                    missing=missing,
                )
            return

        if req.buffer_id is None:
            if req.access == AccessMode.WRITE and not req.spec.source_id:
                region = self._allocate_new_buffer_locked(req)
                if region is None:
                    return
            else:
                buffer = self._resolve_source_buffer_locked(req)
                if buffer is None:
                    return
                req.buffer_id = buffer.buffer_id

        buffer = self._buffers[req.buffer_id]
        self._validate_view_locked(req, buffer)

        if self._try_acquire_locked(req, buffer):
            req.state = RequestState.RUNNING
            req.blocked_reason = None
            req.wait_signature = None
            region_info = buffer.region.info() if buffer.region is not None else req.last_region
            req.last_region = region_info
            if req.access == AccessMode.READ:
                action = "region_shared"
            elif req.spec.source_id:
                action = "write_granted"
            else:
                action = "write_started"
            self._emit(
                action,
                batch_id=req.batch_id,
                request_id=req.request_id,
                buffer_id=buffer.buffer_id,
                region=region_info,
                granted=True,
                access=req.access.value,
                active_readers=buffer.reader_count(),
                active_writer=buffer.writer_id,
            )
            return

        if req.access == AccessMode.WRITE:
            reason = "writer_active" if buffer.writer_id is not None else "readers_drain"
            action = "write_waiting"
        elif buffer.writer_id is not None:
            reason = "writer_active"
            action = "read_waiting"
        else:
            reason = "write_pending_fair_queue"
            action = "read_waiting"
        if req.blocked_reason != reason:
            req.blocked_reason = reason
            req.wait_signature = "lock:" + reason
            self._emit(
                action,
                batch_id=req.batch_id,
                request_id=req.request_id,
                buffer_id=buffer.buffer_id,
                region=buffer.info(),
                granted=False,
                reason=reason,
                active_readers=buffer.reader_count(),
                active_writer=buffer.writer_id,
                waiting_writers=list(buffer.waiting_writers),
            )

    def _resolve_source_buffer_locked(self, req: RequestRecord) -> Buffer | None:
        source_id = req.spec.source_id
        if source_id is None:
            raise ValidationError(f"{req.request_id!r} has no source_id")
        buffer = self._buffers.get(source_id)
        if buffer is None:
            source_req = self._requests.get(source_id)
            if source_req is not None and source_req.buffer_id is not None:
                buffer = self._buffers.get(source_req.buffer_id)
        if buffer is None:
            raise UnknownRequestError(source_id)
        if not buffer.alive or buffer.region is None:
            raise BufferUnavailableError(f"source buffer {source_id!r} was evicted or released")
        return buffer

    def _validate_view_locked(self, req: RequestRecord, buffer: Buffer) -> None:
        region = buffer.region
        if region is None:
            raise BufferUnavailableError(f"buffer {buffer.buffer_id!r} has no active region")
        source_format = region.layout.format
        if req.spec.available_formats and source_format not in req.spec.available_formats:
            from .errors import FormatConflictError

            raise FormatConflictError(
                f"request {req.request_id!r} accepts {list(req.spec.available_formats)!r}, "
                f"but source region {region.region_id!r} uses {source_format!r}"
            )
        needed_logical = req.spec.shape.num_elements * region.layout.itemsize
        if needed_logical > region.layout.logical_bytes:
            from .errors import ShapeMismatchError

            raise ShapeMismatchError(
                f"request {req.request_id!r} needs {needed_logical} logical bytes; "
                f"region {region.region_id!r} provides {region.layout.logical_bytes}"
            )

    def _try_acquire_locked(self, req: RequestRecord, buffer: Buffer) -> bool:
        if req.access == AccessMode.READ:
            if buffer.writer_id is not None and buffer.writer_id != req.request_id:
                return False
            if self.fair_writer_queue and buffer.waiting_writers:
                oldest_id = buffer.waiting_writers[0]
                oldest = self._requests.get(oldest_id)
                if oldest is not None and req.created_sequence > oldest.created_sequence:
                    return False
            buffer.leases[req.request_id] = self._lease_locked(req, buffer.region.region_id)  # type: ignore[union-attr]
            buffer.last_used_sequence = self._sequence
            return True

        if buffer.writer_id == req.request_id:
            return True
        if not buffer.leases and buffer.writer_id is None:
            buffer.writer_id = req.request_id
            if req.request_id in buffer.waiting_writers:
                buffer.waiting_writers.remove(req.request_id)
            buffer.last_used_sequence = self._sequence
            return True
        if req.request_id not in buffer.waiting_writers:
            buffer.waiting_writers.append(req.request_id)
        return False

    def _lease_locked(self, req: RequestRecord, region_id: str) -> Any:
        from .internal import Lease

        return Lease(
            request_id=req.request_id,
            mode=req.access,
            region_id=region_id,
            sequence=self._sequence,
        )

    # ------------------------------------------------------- memory placement

    def _place_region_locked(
        self,
        buffer_id: str,
        binding: Any,
        *,
        generation: int,
        movable: bool,
        evictable: bool,
    ) -> Region | None:
        region_id = self._next_region_id()
        try:
            block = self.pool.allocate(region_id, binding.aligned_size, binding.alignment)
        except MemoryError:
            return None
        return Region(
            region_id=region_id,
            buffer_id=buffer_id,
            generation=generation,
            offset=block.offset,
            size=block.size,
            alignment=block.alignment,
            layout=binding,
            movable=movable,
            evictable=evictable,
        )

    def _allocate_new_buffer_locked(self, req: RequestRecord) -> Region | None:
        spec = req.spec
        batch = self._batches[req.batch_id]
        binding = batch.layout.bind(
            spec.shape,
            spec.available_formats,
            policy=self.format_policy,
        )
        req.binding = binding
        buffer_id = spec.buffer_id or req.request_id
        if buffer_id in self._buffers:
            raise DuplicateRequestError(f"buffer_id already exists: {buffer_id!r}")

        region = self._allocate_region_with_pressure_locked(req, buffer_id, binding)
        if region is None:
            return None

        buffer = Buffer(
            buffer_id=buffer_id,
            batch_id=req.batch_id,
            producer_id=req.request_id,
            layout=batch.layout,
            binding=binding,
            region=region,
            movable=spec.movable,
            evictable=spec.evictable,
        )
        self._buffers[buffer_id] = buffer
        batch.buffer_ids.append(buffer_id)
        req.buffer_id = buffer_id
        info = region.info()
        req.last_region = info
        self._emit(
            "region_allocated",
            batch_id=req.batch_id,
            request_id=req.request_id,
            buffer_id=buffer_id,
            region=info,
            model_version=batch.layout.version,
            logical_bytes=binding.logical_bytes,
            aligned_size=binding.aligned_size,
            selected_format=binding.format,
        )
        return region

    def _allocate_region_with_pressure_locked(
        self,
        req: RequestRecord,
        buffer_id: str,
        binding: Any,
    ) -> Region | None:
        region = self._place_region_locked(
            buffer_id,
            binding,
            generation=1,
            movable=req.spec.movable,
            evictable=req.spec.evictable,
        )
        if region is not None:
            return region

        needed = binding.aligned_size
        self._emit(
            "memory_pressure",
            batch_id=req.batch_id,
            request_id=req.request_id,
            buffer_id=buffer_id,
            granted=False,
            needed_bytes=needed,
            capacity=self.memory_limit,
            free_bytes=self.pool.free_bytes,
            largest_free_block=self.pool.largest_free_block(binding.alignment),
        )

        if needed > self.memory_limit:
            error = MemoryPressureError(
                f"allocation of {needed} bytes exceeds pool capacity {self.memory_limit}",
                needed_bytes=needed,
                capacity=self.memory_limit,
                free_bytes=self.pool.free_bytes,
            )
            req.pressure_error = error
            self._emit_allocation_blocked_locked(req, buffer_id, binding, error)
            return None

        self._evict_under_pressure_locked(req)
        region = self._place_region_locked(
            buffer_id,
            binding,
            generation=1,
            movable=req.spec.movable,
            evictable=req.spec.evictable,
        )
        if region is not None:
            self._emit("pressure_resolved_by_eviction", request_id=req.request_id)
            return region

        for candidate in self._relocation_candidates_locked():
            if self._relocate_buffer_locked(candidate, req.request_id):
                region = self._place_region_locked(
                    buffer_id,
                    binding,
                    generation=1,
                    movable=req.spec.movable,
                    evictable=req.spec.evictable,
                )
                if region is not None:
                    self._emit("pressure_resolved_by_relocation", request_id=req.request_id)
                    return region

        error = MemoryPressureError(
            f"insufficient memory after eviction/relocation for {needed} bytes",
            needed_bytes=needed,
            capacity=self.memory_limit,
            free_bytes=self.pool.free_bytes,
        )
        req.pressure_error = error
        self._emit_allocation_blocked_locked(req, buffer_id, binding, error)
        return None

    def _emit_allocation_blocked_locked(
        self,
        req: RequestRecord,
        buffer_id: str,
        binding: Any,
        error: MemoryPressureError,
    ) -> None:
        req.blocked_reason = "memory_pressure"
        req.wait_signature = "memory_pressure"
        self._emit(
            "allocation_blocked",
            batch_id=req.batch_id,
            request_id=req.request_id,
            buffer_id=buffer_id,
            granted=False,
            needed_bytes=binding.aligned_size,
            capacity=self.memory_limit,
            free_bytes=self.pool.free_bytes,
            error=str(error),
        )

    def _pressure_candidates_locked(self) -> list[Buffer]:
        candidates = [
            buf
            for buf in self._buffers.values()
            if buf.alive
            and buf.region is not None
            and not buf.leases
            and buf.writer_id is None
        ]
        return sorted(candidates, key=lambda b: (b.last_used_sequence, b.buffer_id))

    def _evict_under_pressure_locked(self, requesting_req: RequestRecord) -> None:
        needed = requesting_req.binding.aligned_size if requesting_req.binding else 1
        alignment = requesting_req.binding.alignment if requesting_req.binding else 1
        for buf in self._pressure_candidates_locked():
            if not buf.evictable:
                continue
            if self.pool.can_allocate(needed, alignment):
                return
            self._discard_buffer_locked(
                buf,
                ReleaseReason.MEMORY_PRESSURE_EVICTION.value,
                request_id=requesting_req.request_id,
            )

    def _relocation_candidates_locked(self) -> list[Buffer]:
        return [
            buf
            for buf in self._pressure_candidates_locked()
            if buf.movable and buf.region is not None and buf.region.movable
        ]

    def _relocate_buffer_locked(self, buf: Buffer, requesting_request_id: str | None) -> bool:
        old = buf.region
        if old is None or buf.leases or buf.writer_id is not None:
            return False
        offset = self.pool.find_placement(old.size, old.alignment)
        if offset is None:
            return False

        new = self._place_region_locked(
            buf.buffer_id,
            old.layout,
            generation=old.generation + 1,
            movable=old.movable,
            evictable=old.evictable,
        )
        if new is None:
            return False
        new.payload = old.payload
        self.pool.free(old.region_id)
        old.active = False
        old_info = old.info()
        buf.history.append(old_info)
        buf.region = new
        buf.last_used_sequence = self._sequence

        seq = self._next_sequence()
        release = ReleaseRecord(
            sequence=seq,
            release_kind="region",
            region=old_info,
            reason=ReleaseReason.MEMORY_PRESSURE_RELOCATION.value,
            request_id=requesting_request_id,
            buffer_id=buf.buffer_id,
            replacement_region_id=new.region_id,
        )
        step = StepReport(
            sequence=seq,
            action="region_reallocated",
            batch_id=buf.batch_id,
            request_id=requesting_request_id,
            buffer_id=buf.buffer_id,
            region=new.info(),
            release=release,
            model_version=new.layout.model_version,
            detail={
                "old_region_id": old.region_id,
                "old_offset": old.offset,
                "new_region_id": new.region_id,
                "new_offset": new.offset,
                "payload_copied": new.payload is not None,
            },
        )
        self.trace.append(step)
        return True

    # ------------------------------------------------- completion/cancel/release

    def _remove_waiter_locked(self, req: RequestRecord) -> None:
        if req.buffer_id is None:
            return
        buf = self._buffers.get(req.buffer_id)
        if buf is not None and req.request_id in buf.waiting_writers:
            buf.waiting_writers.remove(req.request_id)

    def _emit_lease_release_locked(self, req: RequestRecord, reason: str) -> StepReport | None:
        if req.buffer_id is None:
            return None
        buf = self._buffers.get(req.buffer_id)
        if buf is None:
            return None
        lease = buf.leases.pop(req.request_id, None)
        was_writer = buf.writer_id == req.request_id
        if was_writer:
            buf.writer_id = None
        if lease is None and not was_writer:
            return None
        region_info = buf.region.info() if buf.region is not None else req.last_region
        if region_info is None:  # pragma: no cover - invariant: leased region stays placed
            return None
        seq = self._next_sequence()
        record = ReleaseRecord(
            sequence=seq,
            release_kind="lease",
            region=region_info,
            reason=reason,
            request_id=req.request_id,
            buffer_id=buf.buffer_id,
        )
        step = StepReport(
            sequence=seq,
            action="lease_released",
            batch_id=req.batch_id,
            request_id=req.request_id,
            buffer_id=buf.buffer_id,
            region=region_info,
            release=record,
            model_version=region_info.model_version,
        )
        self.trace.append(step)
        req.last_region = region_info
        return step

    def _release_region_locked(
        self,
        buf: Buffer,
        reason: str,
        *,
        request_id: str | None = None,
        replacement_region_id: str | None = None,
        action: str = "region_released",
    ) -> StepReport | None:
        old = buf.region
        if old is None:
            return None
        self.pool.free(old.region_id)
        old.active = False
        region_info = old.info()
        buf.history.append(region_info)
        buf.region = None

        seq = self._next_sequence()
        record = ReleaseRecord(
            sequence=seq,
            release_kind="region",
            region=region_info,
            reason=reason,
            request_id=request_id,
            buffer_id=buf.buffer_id,
            replacement_region_id=replacement_region_id,
        )
        step = StepReport(
            sequence=seq,
            action=action,
            batch_id=buf.batch_id,
            request_id=request_id,
            buffer_id=buf.buffer_id,
            region=region_info,
            release=record,
            model_version=region_info.model_version,
        )
        self.trace.append(step)
        return step

    def _discard_buffer_locked(
        self,
        buf: Buffer,
        reason: str,
        *,
        request_id: str | None = None,
    ) -> StepReport | None:
        step = self._release_region_locked(buf, reason, request_id=request_id)
        buf.alive = False
        buf.released = True
        return step

    def _build_receipt_locked(
        self,
        req: RequestRecord,
        status: RequestState,
        sequence: int,
        reason: str | None = None,
    ) -> CompletionReceipt:
        region = req.last_region
        if req.buffer_id is not None:
            buf = self._buffers.get(req.buffer_id)
            if buf is not None and buf.region is not None:
                region = buf.region.info()
        model_version = None
        layout_key = None
        selected_format = None
        if req.binding is not None:
            model_version = req.binding.model_version
            layout_key = req.binding.layout_key
            selected_format = req.binding.format
        elif region is not None:
            model_version = region.model_version
            layout_key = region.layout_key
            selected_format = region.format
        if model_version is None:
            model_version = self._batches[req.batch_id].layout.version

        receipt = CompletionReceipt(
            receipt_id=req.spec.receipt_id or f"rcpt-{req.request_id}-{sequence}",
            request_id=req.request_id,
            batch_id=req.batch_id,
            status=status,
            region=region,
            model_version=model_version,
            layout_key=layout_key,
            selected_format=selected_format,
            submitted_sequence=req.created_sequence,
            completed_sequence=sequence,
            depends_on=tuple(req.spec.depends_on),
            reason=reason,
        )
        req.receipt = receipt
        self.receipts[req.request_id] = receipt
        self._queue_callback(receipt, req.spec.on_complete)
        return receipt

    def complete(
        self,
        request_id: str,
        payload: Any = _UNSET,
    ) -> SchedulerResult:
        """Mark a running stage complete and pump waiting branches/writers."""

        with self._lock:
            req = self._requests.get(request_id)
            if req is None:
                raise UnknownRequestError(request_id)
            before = len(self.trace)

            if req.state == RequestState.COMPLETED:
                result = self._result(
                    "complete",
                    before,
                    req=req,
                    ok=True,
                    receipt=req.receipt,
                    data={"changed": False, "duplicate": True},
                )
                queued = self._callbacks
                self._callbacks = []
            elif req.state in {RequestState.CANCELLED, RequestState.FAILED}:
                raise InvalidStateError(
                    f"request {request_id!r} is already {req.state.value}: {req.terminal_reason}"
                )
            elif req.state == RequestState.PENDING:
                raise InvalidStateError(
                    f"request {request_id!r} is not running; blocked by {req.blocked_reason!r}"
                )
            else:
                if payload is not _UNSET and req.buffer_id is not None:
                    buf = self._buffers.get(req.buffer_id)
                    if buf is not None and buf.region is not None:
                        buf.region.payload = payload
                        buf.last_used_sequence = self._sequence

                if req.access == AccessMode.READ:
                    reason = (
                        ReleaseReason.BRANCH_COMPLETED.value
                        if req.spec.source_id
                        else ReleaseReason.READER_COMPLETED.value
                    )
                else:
                    reason = ReleaseReason.WRITER_COMPLETED.value
                self._emit_lease_release_locked(req, reason)
                req.state = RequestState.COMPLETED
                req.terminal_reason = None
                step = self._emit(
                    "request_completed",
                    batch_id=req.batch_id,
                    request_id=req.request_id,
                    buffer_id=req.buffer_id,
                    region=req.last_region,
                )
                req.terminal_sequence = step.sequence
                receipt = self._build_receipt_locked(
                    req, RequestState.COMPLETED, step.sequence
                )
                self._pump_pending_locked()
                self._finalize_batches_locked()
                result = self._result("complete", before, req=req, receipt=receipt)
                queued = self._callbacks
                self._callbacks = []

        self._flush_callbacks(queued)
        return result

    def cancel_branch(
        self,
        branch_id: str,
        reason: str | None = None,
        *,
        cancel_dependents: bool = True,
    ) -> SchedulerResult:
        """Cancel one branch/request and, by default, its dependent lineage."""

        return self.cancel_request(
            branch_id,
            reason=reason,
            cancel_dependents=cancel_dependents,
            operation="cancel_branch",
        )

    def cancel_request(
        self,
        request_id: str,
        reason: str | None = None,
        *,
        cancel_dependents: bool = True,
        operation: str = "cancel_request",
    ) -> SchedulerResult:
        pending_reason: str | None = None
        with self._lock:
            req = self._requests.get(request_id)
            if req is None:
                raise UnknownRequestError(request_id)
            before = len(self.trace)

            if req.state in {
                RequestState.COMPLETED,
                RequestState.CANCELLED,
                RequestState.FAILED,
            }:
                self._emit(
                    "cancel_ignored",
                    batch_id=req.batch_id,
                    request_id=req.request_id,
                    region=req.last_region,
                    terminal_state=req.state.value,
                )
                result = self._result(
                    operation,
                    before,
                    req=req,
                    ok=False,
                    data={"changed": False, "terminal_state": req.state.value},
                )
                queued = self._callbacks
                self._callbacks = []
            else:
                root_reason = reason or (
                    ReleaseReason.BRANCH_CANCELLED.value
                    if req.access == AccessMode.READ and req.spec.source_id
                    else ReleaseReason.REQUEST_CANCELLED.value
                )
                targets = [req]
                if cancel_dependents:
                    targets.extend(self._dependent_tree_locked(req.request_id))
                receipts: list[CompletionReceipt] = []
                for index, target in enumerate(targets):
                    if target.state in {
                        RequestState.COMPLETED,
                        RequestState.CANCELLED,
                        RequestState.FAILED,
                    }:
                        continue
                    target_reason = (
                        root_reason if index == 0 else ReleaseReason.DEPENDENCY_CANCELLED.value
                    )
                    receipt = self._cancel_one_locked(target, target_reason)
                    if receipt is not None:
                        receipts.append(receipt)

                # If the cancelled producer had active branch leases, the first
                # pass could not discard its region.  The dependent tree has now
                # been cancelled, so release it before pumping replacements.
                if cancel_dependents and req.buffer_id is not None:
                    producer_buffer = self._buffers.get(req.buffer_id)
                    if (
                        producer_buffer is not None
                        and producer_buffer.alive
                        and producer_buffer.region is not None
                        and producer_buffer.producer_id == req.request_id
                        and not producer_buffer.leases
                        and producer_buffer.writer_id is None
                    ):
                        self._discard_buffer_locked(
                            producer_buffer,
                            root_reason,
                            request_id=req.request_id,
                        )

                self._pump_pending_locked()
                self._finalize_batches_locked()
                primary = receipts[0] if receipts else req.receipt
                result = self._result(
                    operation,
                    before,
                    req=req,
                    ok=True,
                    receipt=primary,
                    receipts=receipts,
                    data={
                        "changed": True,
                        "cancelled_count": len(receipts),
                        "cancel_dependents": cancel_dependents,
                    },
                )
                queued = self._callbacks
                self._callbacks = []

        self._flush_callbacks(queued)
        return result

    def _dependent_tree_locked(self, root_id: str) -> list[RequestRecord]:
        result: list[RequestRecord] = []
        seen = {root_id}
        frontier = [root_id]
        while frontier:
            current = frontier.pop(0)
            for req in sorted(self._requests.values(), key=lambda item: item.created_sequence):
                if current in req.spec.depends_on and req.request_id not in seen:
                    seen.add(req.request_id)
                    result.append(req)
                    frontier.append(req.request_id)
        return result

    def _cancel_one_locked(self, req: RequestRecord, reason: str) -> CompletionReceipt | None:
        self._remove_waiter_locked(req)
        self._emit_lease_release_locked(req, reason)

        # Cancelled produced data is invalid.  Release the physical output area
        # immediately when no branch lease remains so pressure allocation can
        # reuse it; completed producers are retained for their readers.
        if req.buffer_id is not None:
            buf = self._buffers.get(req.buffer_id)
            if (
                buf is not None
                and buf.alive
                and buf.region is not None
                and buf.producer_id == req.request_id
                and not buf.leases
                and buf.writer_id is None
            ):
                self._discard_buffer_locked(buf, reason, request_id=req.request_id)

        req.state = RequestState.CANCELLED
        req.blocked_reason = None
        req.terminal_reason = reason
        step = self._emit(
            "request_cancelled",
            batch_id=req.batch_id,
            request_id=req.request_id,
            buffer_id=req.buffer_id,
            region=req.last_region,
            reason=reason,
        )
        req.terminal_sequence = step.sequence
        return self._build_receipt_locked(
            req, RequestState.CANCELLED, step.sequence, reason=reason
        )

    def _fail_request_locked(self, req: RequestRecord, error: BaseException) -> None:
        if req.state in {RequestState.COMPLETED, RequestState.CANCELLED, RequestState.FAILED}:
            return
        self._remove_waiter_locked(req)
        self._emit_lease_release_locked(req, "request_failed")
        req.state = RequestState.FAILED
        req.blocked_reason = None
        req.pressure_error = error if isinstance(error, MemoryPressureError) else req.pressure_error
        req.terminal_reason = str(error)
        step = self._emit(
            "request_failed",
            batch_id=req.batch_id,
            request_id=req.request_id,
            buffer_id=req.buffer_id,
            region=req.last_region,
            error_type=type(error).__name__,
            error=str(error),
        )
        req.terminal_sequence = step.sequence
        self._build_receipt_locked(
            req, RequestState.FAILED, step.sequence, reason=str(error)
        )

    def _finalize_batches_locked(self) -> None:
        for batch in self._batches.values():
            if not batch.sealed:
                continue
            records = [self._requests[rid] for rid in batch.request_ids if rid in self._requests]
            all_terminal = all(
                req.state in {RequestState.COMPLETED, RequestState.CANCELLED, RequestState.FAILED}
                for req in records
            )
            if not batch.completed and all_terminal:
                batch.completed = True
                self._emit(
                    "batch_completed",
                    batch_id=batch.batch_id,
                    model_version=batch.layout.version,
                    request_count=len(records),
                    completed=[r.request_id for r in records if r.state == RequestState.COMPLETED],
                    cancelled=[r.request_id for r in records if r.state == RequestState.CANCELLED],
                    failed=[r.request_id for r in records if r.state == RequestState.FAILED],
                )

            if not batch.completed:
                continue
            if not batch.auto_release:
                batch.released = True
                continue
            if batch.released:
                continue

            for buffer_id in list(batch.buffer_ids):
                buf = self._buffers.get(buffer_id)
                if buf is None or not buf.alive or buf.region is None:
                    continue
                if buf.leases or buf.writer_id is not None:
                    continue
                self._discard_buffer_locked(
                    buf,
                    ReleaseReason.BATCH_COMPLETED.value,
                )
            all_discarded = all(
                (self._buffers[bid].region is None or not self._buffers[bid].alive)
                for bid in batch.buffer_ids
                if bid in self._buffers
            )
            if all_discarded:
                batch.released = True
                self._emit(
                    "batch_memory_released",
                    batch_id=batch.batch_id,
                    model_version=batch.layout.version,
                )

    def release_buffer(self, buffer_id: str, *, reason: str | None = None) -> SchedulerResult:
        with self._lock:
            before = len(self.trace)
            buf = self._buffers.get(buffer_id)
            if buf is None:
                raise UnknownBufferError(buffer_id)
            if buf.leases or buf.writer_id is not None:
                raise BufferBusyError(
                    f"buffer {buffer_id!r} has {len(buf.leases)} lease(s), writer={buf.writer_id!r}"
                )
            if not buf.alive or buf.region is None:
                result = self._result(
                    "release_buffer",
                    before,
                    ok=False,
                    data={"changed": False, "buffer_id": buffer_id},
                )
                return result
            step = self._discard_buffer_locked(
                buf, reason or ReleaseReason.CALLER_RELEASED.value
            )
            result = self._result(
                "release_buffer",
                before,
                ok=True,
                data={"changed": step is not None, "buffer_id": buffer_id},
            )
        return result

    def reclaim_memory(self, needed_bytes: int = 0) -> SchedulerResult:
        """Evict idle, evictable buffers until at least ``needed_bytes`` free."""

        with self._lock:
            before = len(self.trace)
            evicted = 0
            candidates = self._pressure_candidates_locked()
            for buf in candidates:
                if not buf.evictable:
                    continue
                if needed_bytes and self.pool.free_bytes >= needed_bytes:
                    break
                if self._discard_buffer_locked(
                    buf, ReleaseReason.MEMORY_PRESSURE_EVICTION.value
                ):
                    evicted += 1
            result = self._result(
                "reclaim_memory",
                before,
                data={
                    "evicted_count": evicted,
                    "needed_bytes": needed_bytes,
                    "free_bytes": self.pool.free_bytes,
                    "changed": evicted > 0,
                },
            )
        return result

    # ---------------------------------------------------------------- queries

    def _info_locked(self, req: RequestRecord) -> RequestInfo:
        region = req.last_region
        active_lease = False
        if req.buffer_id is not None:
            buf = self._buffers.get(req.buffer_id)
            if buf is not None:
                if buf.region is not None and req.state == RequestState.RUNNING:
                    region = buf.region.info()
                active_lease = req.request_id in buf.leases or buf.writer_id == req.request_id
        return RequestInfo(
            request_id=req.request_id,
            batch_id=req.batch_id,
            state=req.state,
            access=req.access,
            depends_on=tuple(req.spec.depends_on),
            source_id=req.spec.source_id,
            buffer_id=req.buffer_id,
            region=region,
            model_version=req.binding.model_version if req.binding else (
                region.model_version if region else self._batches[req.batch_id].layout.version
            ),
            layout_key=req.binding.layout_key if req.binding else (
                region.layout_key if region else None
            ),
            selected_format=req.binding.format if req.binding else (
                region.format if region else None
            ),
            blocked_reason=req.blocked_reason,
            terminal_reason=req.terminal_reason,
            active_lease=active_lease,
        )

    def get_request(self, request_id: str) -> RequestInfo:
        with self._lock:
            req = self._requests.get(request_id)
            if req is None:
                raise UnknownRequestError(request_id)
            return self._info_locked(req)

    def current_region(self, buffer_id: str) -> Any:
        with self._lock:
            buf = self._buffers.get(buffer_id)
            if buf is None:
                raise UnknownBufferError(buffer_id)
            return buf.info()

    def get_region(self, region_id: str) -> Any:
        with self._lock:
            for buf in self._buffers.values():
                if buf.region is not None and buf.region.region_id == region_id:
                    return buf.region.info()
                for old in buf.history:
                    if old.region_id == region_id:
                        return old
        from .errors import UnknownBufferError

        raise UnknownBufferError(region_id)

    def active_readers(self, buffer_id: str) -> tuple[str, ...]:
        with self._lock:
            buf = self._buffers.get(buffer_id)
            if buf is None:
                raise UnknownBufferError(buffer_id)
            return tuple(
                request_id
                for request_id, lease in buf.leases.items()
                if lease.mode == AccessMode.READ
            )

    def waiting_writers(self, buffer_id: str) -> tuple[str, ...]:
        with self._lock:
            buf = self._buffers.get(buffer_id)
            if buf is None:
                raise UnknownBufferError(buffer_id)
            return tuple(buf.waiting_writers)

    def _resolve_buffer_identifier_locked(self, identifier: str) -> Buffer:
        buf = self._buffers.get(identifier)
        if buf is not None:
            return buf
        req = self._requests.get(identifier)
        if req is not None and req.buffer_id is not None:
            buf = self._buffers.get(req.buffer_id)
            if buf is not None:
                return buf
        raise UnknownBufferError(identifier)

    def write_payload(self, buffer_id: str, payload: Any) -> None:
        """Store a synthetic payload in the current placed region.

        A request id may be supplied as well as the stable buffer id.
        """

        with self._lock:
            buf = self._resolve_buffer_identifier_locked(buffer_id)
            if not buf.alive or buf.region is None:
                raise BufferUnavailableError(f"buffer {buffer_id!r} is not placed")
            buf.region.payload = payload

    def read_payload(self, buffer_id: str) -> Any:
        """Return the synthetic payload object without copying it."""

        with self._lock:
            buf = self._resolve_buffer_identifier_locked(buffer_id)
            if not buf.alive or buf.region is None:
                raise BufferUnavailableError(f"buffer {buffer_id!r} is not placed")
            return buf.region.payload

    def steps_for(self, request_id: str) -> list[StepReport]:
        with self._lock:
            if request_id not in self._requests:
                raise UnknownRequestError(request_id)
            return [step for step in self.trace if step.request_id == request_id]

    def memory_snapshot(self) -> dict[str, Any]:
        with self._lock:
            snapshot = self.pool.snapshot()
            snapshot["buffers"] = [
                {
                    "buffer_id": buf.buffer_id,
                    "batch_id": buf.batch_id,
                    "alive": buf.alive,
                    "region_id": buf.region.region_id if buf.region else None,
                    "offset": buf.region.offset if buf.region else None,
                    "size": buf.region.size if buf.region else None,
                    "readers": buf.reader_count(),
                    "writer_id": buf.writer_id,
                    "waiting_writers": list(buf.waiting_writers),
                    "evictable": buf.evictable,
                    "movable": buf.movable,
                    "model_version": buf.binding.model_version,
                }
                for buf in self._buffers.values()
            ]
            return snapshot

    def batch_snapshot(self, batch_id: str) -> dict[str, Any]:
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise UnknownBatchError(batch_id)
            return {
                "batch_id": batch.batch_id,
                "model_version": batch.layout.version,
                "alignment": batch.layout.alignment,
                "sealed": batch.sealed,
                "completed": batch.completed,
                "released": batch.released,
                "auto_release": batch.auto_release,
                "request_ids": list(batch.request_ids),
                "buffer_ids": list(batch.buffer_ids),
            }

    def audit(self) -> list[dict[str, Any]]:
        """Return the complete step trace as plain JSON-like dictionaries."""

        with self._lock:
            return [step.to_dict() for step in self.trace]
