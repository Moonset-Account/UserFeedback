"""Shared synthetic fixtures for the profiler tests.

All data here is invented: a module ``libengine`` with two builds, a small
host executable and a second module.  Nothing is loaded from disk, and no
real binary, process or profile capture is involved.
"""

from __future__ import annotations

from cpu_sampler import (
    FunctionRange,
    InlineFrame,
    LoadEvent,
    ModuleImage,
    SampleEvent,
    SymbolTable,
    UnloadEvent,
)

# Base addresses are deliberately small and round so arithmetic in tests is
# easy to follow.
ENGINE_V1_BASE = 0x10000
ENGINE_V2_BASE = 0x10000  # reloaded at the *same* base on purpose
API_BASE = 0x40000
HOST_BASE = 0x70000


def engine_v1() -> ModuleImage:
    """First build of ``libengine``.

    Layout (module-relative):

    ========  ====================================================
    0x0000    ``engine_loop`` [0x0000, 0x0040)
    0x0010      inline ``loop_step`` [0x0010, 0x0030)
    0x0018        inline ``loop_body`` [0x0018, 0x0028)
    0x0040    ``engine_flush`` [0x0040, 0x0060)
    0x0080    ``engine_idle`` [0x0080, 0x0090)
    ========  ====================================================
    """

    return ModuleImage(
        module="libengine",
        version="1.0",
        size=0x1000,
        build_id="engine-build-1",
        functions=(
            FunctionRange(
                name="engine_loop",
                lo=0x0000,
                hi=0x0040,
                inlines=(
                    InlineFrame(
                        name="loop_step",
                        lo=0x0010,
                        hi=0x0030,
                        children=(
                            InlineFrame(name="loop_body", lo=0x0018, hi=0x0028),
                        ),
                    ),
                ),
            ),
            FunctionRange(name="engine_flush", lo=0x0040, hi=0x0060),
            FunctionRange(name="engine_idle", lo=0x0080, hi=0x0090),
        ),
    )


def engine_v2() -> ModuleImage:
    """Second build of ``libengine``: same module, different code layout.

    ``engine_flush`` is gone and the name at 0x0040 is now ``engine_drain``,
    so an address reused across the swap must resolve differently.
    """

    return ModuleImage(
        module="libengine",
        version="2.0",
        size=0x1000,
        build_id="engine-build-2",
        functions=(
            FunctionRange(
                name="engine_loop",
                lo=0x0000,
                hi=0x0038,
                inlines=(
                    InlineFrame(name="loop_step", lo=0x0010, hi=0x0020),
                ),
            ),
            FunctionRange(name="engine_drain", lo=0x0040, hi=0x0060),
            FunctionRange(name="engine_alloc", lo=0x0080, hi=0x00A0),
        ),
    )


def libapi() -> ModuleImage:
    """A small unrelated module, used to prove per-pid attribution."""

    return ModuleImage(
        module="libapi",
        version="3.1",
        size=0x800,
        functions=(
            FunctionRange(name="api_dispatch", lo=0x0100, hi=0x0140),
            FunctionRange(name="api_wait", lo=0x0200, hi=0x0220),
        ),
    )


def host() -> ModuleImage:
    """The host executable."""

    return ModuleImage(
        module="host",
        version="0.9",
        size=0x1000,
        functions=(
            FunctionRange(name="main", lo=0x0000, hi=0x0100),
            FunctionRange(name="run", lo=0x0100, hi=0x0200),
        ),
    )


def symbol_table() -> SymbolTable:
    return SymbolTable([engine_v1(), engine_v2(), libapi(), host()])


def load(
    timestamp: int,
    pid: int,
    module: str,
    version: str,
    base: int,
    size: int = 0x1000,
) -> LoadEvent:
    return LoadEvent(
        timestamp=timestamp,
        pid=pid,
        module=module,
        version=version,
        base=base,
        size=size,
    )


def unload(
    timestamp: int,
    pid: int,
    module: str,
    version: str | None = None,
    base: int | None = None,
) -> UnloadEvent:
    return UnloadEvent(
        timestamp=timestamp,
        pid=pid,
        module=module,
        version=version,
        base=base,
    )


def sample(timestamp: int, pid: int, frames) -> SampleEvent:
    return SampleEvent(timestamp=timestamp, pid=pid, frames=tuple(frames))
