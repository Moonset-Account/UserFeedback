"""In-memory model of file service behavior.

The model is replayed along the deterministic topological order derived from
the happens-before graph. It only records *facts* (which handle exists, what
it is bound to, which task validated/canceled/retry-flagged it, the object's
current generation). All authorization verdicts live in ``verifier.py`` and
are reached through happens-before queries, so an ambiguous order becomes an
UNKNOWN rather than an assumption.
"""

from dataclasses import dataclass, field

from .events import (
    Event,
    BIND,
    CANCEL,
    HANDLE_CREATE,
    RETRY,
    VALIDATE_OK,
    WRITE_COMMIT,
)
from .order import OrderGraph


@dataclass
class HandleState:
    handle: str
    created_by: Event
    bound: bool = False
    object_no: str | None = None
    object_gen: int | None = None
    bind_event: Event | None = None
    canceled: bool = False
    cancel_events: list[Event] = field(default_factory=list)
    # task -> list of validation events observed for this handle
    validations_by_task: dict[str, list[Event]] = field(default_factory=dict)
    # task -> list of retry events observed for this handle
    retries_by_task: dict[str, list[Event]] = field(default_factory=dict)
    commits: list[Event] = field(default_factory=list)


@dataclass
class ObjectState:
    object_no: str
    # latest (generation, binding event) known to the model
    latest_gen: int | None = None
    latest_bind: Event | None = None


class FileServiceModel:
    def __init__(self, events: list[Event], graph: OrderGraph):
        self.events = events
        self.graph = graph
        self.by_id = graph.by_id
        self.handles: dict[str, HandleState] = {}
        self.objects: dict[str, ObjectState] = {}
        self._replay()

    def _replay(self) -> None:
        for e in self.graph.topological_replay():
            if e.type == HANDLE_CREATE and e.handle:
                if e.handle not in self.handles:
                    self.handles[e.handle] = HandleState(
                        handle=e.handle, created_by=e
                    )
            elif e.type == BIND and e.handle:
                hs = self.handles.setdefault(
                    e.handle, HandleState(handle=e.handle, created_by=e)
                )
                hs.bound = True
                hs.object_no = e.object_no
                hs.object_gen = e.object_gen
                hs.bind_event = e
                if e.object_no is not None:
                    obj = self.objects.setdefault(
                        e.object_no, ObjectState(e.object_no)
                    )
                    if (
                        e.object_gen is not None
                        and (
                            obj.latest_gen is None
                            or e.object_gen > obj.latest_gen
                        )
                    ):
                        obj.latest_gen = e.object_gen
                        obj.latest_bind = e
            elif e.type == VALIDATE_OK and e.handle and e.task:
                hs = self.handles.setdefault(
                    e.handle, HandleState(handle=e.handle, created_by=e)
                )
                hs.validations_by_task.setdefault(e.task, []).append(e)
            elif e.type == CANCEL and e.handle:
                hs = self.handles.setdefault(
                    e.handle, HandleState(handle=e.handle, created_by=e)
                )
                hs.canceled = True
                hs.cancel_events.append(e)
            elif e.type == RETRY and e.handle and e.task:
                hs = self.handles.setdefault(
                    e.handle, HandleState(handle=e.handle, created_by=e)
                )
                hs.retries_by_task.setdefault(e.task, []).append(e)
            elif e.type == WRITE_COMMIT and e.handle:
                hs = self.handles.setdefault(
                    e.handle, HandleState(handle=e.handle, created_by=e)
                )
                hs.commits.append(e)

    # ------------------------------------------------------------- accessors
    def handle(self, handle: str) -> HandleState | None:
        return self.handles.get(handle)

    def object(self, object_no: str) -> ObjectState | None:
        return self.objects.get(object_no)

    def validations_for(self, handle: str, task: str) -> list[Event]:
        hs = self.handles.get(handle)
        if hs is None:
            return []
        return list(hs.validations_by_task.get(task, ()))

    def retries_for(self, handle: str, task: str) -> list[Event]:
        hs = self.handles.get(handle)
        if hs is None:
            return []
        return list(hs.retries_by_task.get(task, ()))

    def cancels_for(self, handle: str) -> list[Event]:
        hs = self.handles.get(handle)
        if hs is None:
            return []
        return list(hs.cancel_events)
