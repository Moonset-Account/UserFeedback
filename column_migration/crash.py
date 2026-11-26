"""Deterministic/random crash barriers used by tests and the demo child."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional

from .errors import SimulatedCrash


@dataclass(frozen=True)
class CrashEvent:
    seq: int
    point: str
    context: Mapping[str, Any] = field(default_factory=dict)


class CrashController:
    """Records every durable barrier and optionally stops at one event.

    A target is an absolute :attr:`CrashEvent.seq` in the barrier trace.  This
    lets a test first record a complete operation and then replay fresh stores,
    cutting at exactly the same nth barrier.

    ``hard_exit=True`` uses :func:`os._exit`; this is used by a subprocess test
    to prove that no in-process cleanup or deferred write can repair state.
    """

    def __init__(
        self,
        *,
        crash_at_seq: Optional[int] = None,
        hard_exit: bool = False,
        on_event: Optional[Callable[[CrashEvent], None]] = None,
    ) -> None:
        self.crash_at_seq = crash_at_seq
        self.hard_exit = hard_exit
        self.on_event = on_event
        self.events: list[CrashEvent] = []
        self.crashed_event: Optional[CrashEvent] = None

    def check(self, point: str, **context: Any) -> CrashEvent:
        # Keep context JSON-friendly and stable enough for diagnostics.
        clean_context: Dict[str, Any] = {}
        for key, value in context.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                clean_context[key] = value
            else:
                clean_context[key] = repr(value)
        event = CrashEvent(len(self.events), point, dict(clean_context))
        self.events.append(event)
        if self.on_event is not None:
            self.on_event(event)
        if self.crash_at_seq is not None and event.seq == self.crash_at_seq:
            self.crashed_event = event
            if self.hard_exit:
                # 97 is deliberately unusual and asserted by the subprocess.
                os._exit(97)
            raise SimulatedCrash(event)
        return event

    def point_names(self) -> list[str]:
        return [event.point for event in self.events]
