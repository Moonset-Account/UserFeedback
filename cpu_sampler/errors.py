"""Exception hierarchy for the CPU sampling profiler.

Every error raised on purpose by this package derives from
:class:`ProfilerError`, so callers can distinguish "the trace is
inconsistent" from an unrelated bug.
"""

from __future__ import annotations


class ProfilerError(Exception):
    """Base class for every error raised by this component."""


class ValidationError(ProfilerError, ValueError):
    """An input value is malformed (wrong type, out of range, ...)."""


class SymbolTableError(ValidationError):
    """A module image or its inline/function ranges are malformed."""


class DuplicateImageError(SymbolTableError):
    """Two different images claim the same (module, version) pair."""


class InputFormatError(ValidationError):
    """A serialized profile input does not match the documented schema."""


class UnknownImageError(ProfilerError, LookupError):
    """A load event references a module image that was not supplied."""


class MappingConflictError(ValidationError):
    """A load would overlap a mapping that is already active for its process."""


class UnloadWithoutMappingError(ProfilerError, LookupError):
    """An unload does not match any active mapping for its process."""


class MappingIdentityError(ValidationError):
    """An unload names a different module/version than the mapping it hits."""


class AmbiguousMappingError(ProfilerError, LookupError):
    """An unload matches more than one active mapping of the same module."""
