"""CPU sampling profiler with module-version-aware symbolisation.

Public surface::

    from cpu_sampler import (
        SymbolTable, ModuleImage, FunctionRange, InlineFrame,
        LoadEvent, UnloadEvent, SampleEvent,
        Profiler, Profile, parse_trace, build_profile,
        build_call_tree, aggregate_stacks,
    )
"""

from __future__ import annotations

from .errors import (
    AmbiguousMappingError,
    DuplicateImageError,
    InputFormatError,
    MappingConflictError,
    MappingIdentityError,
    ProfilerError,
    SymbolTableError,
    UnknownImageError,
    UnloadWithoutMappingError,
    ValidationError,
)
from .events import (
    EVENT_LOAD,
    EVENT_SAMPLE,
    EVENT_UNLOAD,
    PHASE_ORDER,
    LoadEvent,
    SampleEvent,
    UnloadEvent,
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
from .model import FunctionRange, InlineFrame, ModuleImage, SymbolMatch
from .profiler import RETURN_ADDRESS_ADJUSTMENT, Profile, ProfileResult, Profiler
from .report import CallTreeNode, Hotspot, aggregate_stacks, build_call_tree
from .symbols import SymbolTable
from .trace import build_profile, parse_trace

__version__ = "1.0.0"

__all__ = [
    "__version__",
    # errors
    "ProfilerError",
    "ValidationError",
    "SymbolTableError",
    "DuplicateImageError",
    "InputFormatError",
    "UnknownImageError",
    "MappingConflictError",
    "UnloadWithoutMappingError",
    "MappingIdentityError",
    "AmbiguousMappingError",
    # model
    "InlineFrame",
    "FunctionRange",
    "ModuleImage",
    "SymbolMatch",
    "SymbolTable",
    # events
    "LoadEvent",
    "UnloadEvent",
    "SampleEvent",
    "EVENT_LOAD",
    "EVENT_UNLOAD",
    "EVENT_SAMPLE",
    "PHASE_ORDER",
    "sort_events",
    # mapping / profiler
    "ActiveMapping",
    "MappingTable",
    "Profiler",
    "Profile",
    "ProfileResult",
    "RETURN_ADDRESS_ADJUSTMENT",
    # output
    "ResolvedFrame",
    "ResolvedStack",
    "PLACEHOLDER",
    "ROLE_LEAF",
    "ROLE_CALL_SITE",
    "ROLE_INLINE",
    "ROLE_PLACEHOLDER",
    "CallTreeNode",
    "Hotspot",
    "build_call_tree",
    "aggregate_stacks",
    # input
    "parse_trace",
    "build_profile",
]
