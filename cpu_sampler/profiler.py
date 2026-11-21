"""The profiler: replay events in time order and symbolise every sample.

Address attribution rules (these are the whole point of the component):

1. Events are sorted by ``(timestamp, phase, sequence)`` where
   ``unload < load < sample``.  Input order never matters.
2. A sample frame is attributed to the mapping that is *active for its
   process at the sample's timestamp*.  Since a module can be unloaded and
   another build loaded at the same base, the mapping keeps the identity of
   the image it was created from, so the same absolute address can resolve
   against two different versions at two different moments.
3. The **leaf** frame (last in the root-first stack) holds the sampled
   instruction pointer: it is looked up as-is.
4. Every **other** frame holds a return address, which points *after* the
   call instruction.  One address unit is subtracted before the lookup so
   the lookup lands on the call site inside the caller.
5. The located function's inline chain is expanded outermost → innermost,
   one output frame per level, so the emitted stack is a full root-to-leaf
   expansion.
6. Addresses that cannot be located are kept as placeholder frames; frames
   are never dropped, so recursion that shows up twice keeps both copies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from .errors import ValidationError
from .events import (
    EVENT_LOAD,
    EVENT_SAMPLE,
    EVENT_UNLOAD,
    SampleEvent,
    sort_events,
)
from .frames import (
    PLACEHOLDER,
    ROLE_CALL_SITE,
    ROLE_INLINE,
    ROLE_LEAF,
    ROLE_PLACEHOLDER,
    ResolvedFrame,
    ResolvedStack,
)
from .mapping import ActiveMapping, MappingTable
from .model import SymbolMatch
from .symbols import SymbolTable

__all__ = ["Profile", "ProfileResult", "Profiler"]

#: Each stack frame except the leaf stores a return address, which is one
#: address unit past the call; step back one unit to find the call site.
RETURN_ADDRESS_ADJUSTMENT = 1

# Reason codes recorded on placeholder frames.
REASON_NO_MAPPING = "no-mapping"
REASON_OUTSIDE_IMAGE = "outside-image"
REASON_NO_FUNCTION = "no-function"


@dataclass(frozen=True)
class ProfileResult:
    """Everything produced by replaying one trace."""

    stacks: tuple
    #: Mappings still active when the trace ended, per process.
    live_mappings: tuple = ()
    #: Number of load / unload events that were applied.
    loads: int = 0
    unloads: int = 0
    #: Number of samples that contained at least one placeholder.
    samples_with_placeholders: int = 0

    @property
    def sample_count(self) -> int:
        return len(self.stacks)

    @property
    def placeholder_count(self) -> int:
        return sum(stack.placeholder_count for stack in self.stacks)

    def stacks_for_pid(self, pid: int) -> tuple:
        return tuple(stack for stack in self.stacks if stack.pid == pid)


@dataclass
class Profile:
    """A trace: symbol images plus an unordered stream of events."""

    symbols: SymbolTable
    events: List[object] = field(default_factory=list)

    def add(self, *events: object) -> "Profile":
        self.events.extend(events)
        return self

    def result(self) -> ProfileResult:
        return Profiler(self.symbols).run(self.events)


class Profiler:
    """Replays events against a symbol table and resolves sample stacks."""

    def __init__(self, symbols: SymbolTable) -> None:
        if not isinstance(symbols, SymbolTable):
            raise TypeError(
                f"symbols must be a SymbolTable, got {type(symbols).__name__}"
            )
        self._symbols = symbols

    def run(self, events: Iterable[object]) -> ProfileResult:
        table = MappingTable(self._symbols)
        stacks: List[ResolvedStack] = []
        loads = unloads = 0
        samples_with_placeholders = 0

        for ordered in sort_events(events):
            event = ordered.event
            if event.kind == EVENT_LOAD:
                table.load(event)
                loads += 1
            elif event.kind == EVENT_UNLOAD:
                table.unload(event)
                unloads += 1
            elif event.kind == EVENT_SAMPLE:
                stack = self._resolve_sample(event, table)
                stacks.append(stack)
                if stack.has_placeholders:
                    samples_with_placeholders += 1
            else:  # pragma: no cover - sort_events already rejected this
                raise ValidationError(f"unsupported event kind {event.kind!r}")

        return ProfileResult(
            stacks=tuple(stacks),
            live_mappings=tuple(table),
            loads=loads,
            unloads=unloads,
            samples_with_placeholders=samples_with_placeholders,
        )

    def _resolve_sample(
        self, event: SampleEvent, table: MappingTable
    ) -> ResolvedStack:
        leaf_index = len(event.frames) - 1
        frames: List[ResolvedFrame] = []
        for index, address in enumerate(event.frames):
            frames.extend(
                self._resolve_frame(
                    address=address,
                    index=index,
                    is_leaf=index == leaf_index,
                    pid=event.pid,
                    table=table,
                )
            )
        return ResolvedStack(
            timestamp=event.timestamp, pid=event.pid, frames=tuple(frames)
        )

    def _resolve_frame(
        self,
        *,
        address: int,
        index: int,
        is_leaf: bool,
        pid: int,
        table: MappingTable,
    ) -> List[ResolvedFrame]:
        """Resolve one raw frame into one-or-more output frames."""

        role = ROLE_LEAF if is_leaf else ROLE_CALL_SITE
        lookup = address if is_leaf else address - RETURN_ADDRESS_ADJUSTMENT

        mapping = table.find(pid, lookup)
        if mapping is None:
            reason = (
                REASON_OUTSIDE_IMAGE
                if lookup < 0
                else REASON_NO_MAPPING
            )
            return [
                self._placeholder(
                    index=index,
                    address=address,
                    lookup=lookup,
                    reason=reason,
                    mapping=None,
                )
            ]

        relative = mapping.relative(lookup)
        match = self._symbols.resolve(mapping.module, mapping.version, relative)
        if match is None:
            reason = (
                REASON_OUTSIDE_IMAGE
                if relative < 0 or relative >= mapping.image.size
                else REASON_NO_FUNCTION
            )
            return [
                self._placeholder(
                    index=index,
                    address=address,
                    lookup=lookup,
                    reason=reason,
                    mapping=mapping,
                )
            ]

        return self._expand(
            match=match,
            mapping=mapping,
            index=index,
            address=address,
            lookup=lookup,
            base_role=role,
        )

    @staticmethod
    def _expand(
        *,
        match: SymbolMatch,
        mapping: ActiveMapping,
        index: int,
        address: int,
        lookup: int,
        base_role: str,
    ) -> List[ResolvedFrame]:
        """Emit one output frame per level of the inline chain."""

        frames: List[ResolvedFrame] = []
        for depth, name in enumerate(match.chain):
            role = base_role if depth == 0 else ROLE_INLINE
            frames.append(
                ResolvedFrame(
                    frame_index=index,
                    inline_depth=depth,
                    role=role,
                    address=address,
                    lookup_address=lookup,
                    resolved=True,
                    name=name,
                    chain=match.chain,
                    module=match.module,
                    version=match.version,
                    build_id=match.build_id,
                    base=mapping.base,
                    relative_address=mapping.relative(lookup),
                    reason=None,
                    generation=mapping.generation,
                )
            )
        return frames

    @staticmethod
    def _placeholder(
        *,
        index: int,
        address: int,
        lookup: int,
        reason: str,
        mapping: Optional[ActiveMapping],
    ) -> ResolvedFrame:
        return ResolvedFrame(
            frame_index=index,
            inline_depth=0,
            role=ROLE_PLACEHOLDER,
            address=address,
            lookup_address=lookup,
            resolved=False,
            name=PLACEHOLDER,
            chain=(),
            module=mapping.module if mapping else None,
            version=mapping.version if mapping else None,
            build_id=mapping.image.build_id if mapping else None,
            base=mapping.base if mapping else None,
            relative_address=(
                mapping.relative(lookup) if mapping is not None else None
            ),
            reason=reason,
            generation=mapping.generation if mapping else None,
        )
