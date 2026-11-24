"""Exception hierarchy for the build execution semantics library."""


class SemanticsError(Exception):
    """Base class for every error raised by this package."""


class ValidationError(SemanticsError):
    """The pipeline / snapshot description is malformed (cycle, unknown name...)."""


class LookupTableError(SemanticsError):
    """A step ended with a frozen input tuple that its table does not cover."""


class TraceError(SemanticsError):
    """A replayed trace is not a legal interleaving for the given root."""
