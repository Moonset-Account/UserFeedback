"""Per-process table of active module mappings.

A mapping is created by a load and destroyed by an unload.  Because a
module may be unloaded and a *newer build* loaded at the same base, the
table must never collapse two generations into one: each active mapping
remembers the exact :class:`~cpu_sampler.model.ModuleImage` it came from,
and samples are resolved against that image, not against "whatever image
has this name now".

This is what makes two samples at the same absolute address resolve to
different, individually traceable stacks when the mapping changed between
them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

from .errors import (
    AmbiguousMappingError,
    MappingConflictError,
    MappingIdentityError,
    UnknownImageError,
    UnloadWithoutMappingError,
)
from .events import LoadEvent, UnloadEvent
from .model import ModuleImage
from .symbols import SymbolTable

__all__ = ["ActiveMapping", "MappingTable"]


@dataclass(frozen=True)
class ActiveMapping:
    """One live module mapping in one process."""

    pid: int
    module: str
    version: str
    base: int
    size: int
    image: ModuleImage
    generation: int

    @property
    def end(self) -> int:
        return self.base + self.size

    def contains(self, address: int) -> bool:
        return self.base <= address < self.end

    def relative(self, address: int) -> int:
        return address - self.base

    def describe(self) -> str:
        return (
            f"{self.module}@{self.version} at {self.base:#x}"
            f" (generation {self.generation})"
        )


class MappingTable:
    """The set of active mappings for every process seen so far."""

    def __init__(self, symbols: SymbolTable) -> None:
        self._symbols = symbols
        self._by_pid: Dict[int, List[ActiveMapping]] = {}
        self._generations: Dict[int, int] = {}

    # ----------------------------------------------------------------- mutate
    def load(self, event: LoadEvent) -> ActiveMapping:
        """Apply a load event and return the mapping it created."""

        image = self._symbols.get(event.module, event.version)
        if image is None:
            raise UnknownImageError(
                f"load references {event.module}@{event.version}, "
                f"for which no image was supplied"
            )
        # Validate against the currently active mappings before touching any
        # state, so a rejected load leaves the table exactly as it was.
        new_base, new_end = event.base, event.base + event.size
        for existing in self._by_pid.get(event.pid, ()):
            if existing.module == event.module and existing.base == new_base:
                # The same module is already mapped at this base: refuse
                # instead of guessing which generation the caller meant.
                raise MappingConflictError(
                    f"pid {event.pid}: {event.module}@{event.version} is "
                    f"already loaded as {existing.describe()}"
                )
            if new_base < existing.end and existing.base < new_end:
                raise MappingConflictError(
                    f"pid {event.pid}: load of {event.module}@{event.version} "
                    f"at [{new_base:#x}, {new_end:#x}) overlaps active mapping "
                    f"{existing.describe()}"
                )

        active = self._by_pid.setdefault(event.pid, [])
        generation = self._generations.get(event.pid, 0) + 1
        self._generations[event.pid] = generation
        mapping = ActiveMapping(
            pid=event.pid,
            module=event.module,
            version=event.version,
            base=event.base,
            size=event.size,
            image=image,
            generation=generation,
        )
        active.append(mapping)
        return mapping

    def unload(self, event: UnloadEvent) -> ActiveMapping:
        """Apply an unload event and return the mapping it removed."""

        active = self._by_pid.get(event.pid, [])
        candidates = [m for m in active if m.module == event.module]
        if not candidates:
            raise UnloadWithoutMappingError(
                f"pid {event.pid}: unload of {self._describe_request(event)} "
                f"matches no active mapping"
            )
        if event.base is not None:
            candidates = [m for m in candidates if m.base == event.base]
            if not candidates:
                raise UnloadWithoutMappingError(
                    f"pid {event.pid}: unload of "
                    f"{self._describe_request(event)} matches no active mapping"
                )
        if event.version is not None:
            by_version = [m for m in candidates if m.version == event.version]
            if not by_version:
                raise MappingIdentityError(
                    f"pid {event.pid}: unload names "
                    f"{event.module}@{event.version} but the matching "
                    f"mapping(s) are "
                    + ", ".join(m.describe() for m in candidates)
                )
            candidates = by_version

        if len(candidates) > 1:
            raise AmbiguousMappingError(
                f"pid {event.pid}: unload of {self._describe_request(event)} "
                f"matches {len(candidates)} active mappings; "
                f"add a base or version to disambiguate"
            )

        mapping = candidates[0]
        active.remove(mapping)
        if not active:
            self._by_pid.pop(event.pid, None)
        return mapping

    # ---------------------------------------------------------------- queries
    def find(self, pid: int, address: int) -> Optional[ActiveMapping]:
        """Return the mapping of ``pid`` that covers ``address``."""

        for mapping in self._by_pid.get(pid, ()):
            if mapping.contains(address):
                return mapping
        return None

    def active_for(self, pid: int) -> Tuple[ActiveMapping, ...]:
        return tuple(self._by_pid.get(pid, ()))

    def __iter__(self) -> Iterator[ActiveMapping]:
        for mappings in self._by_pid.values():
            yield from mappings

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _describe_request(event: UnloadEvent) -> str:
        parts = [event.module]
        if event.version is not None:
            parts.append(f"@{event.version}")
        if event.base is not None:
            parts.append(f"at {event.base:#x}")
        return "".join(parts)
