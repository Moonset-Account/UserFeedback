"""Error hierarchy for :mod:`relanalysis`."""


class RelAnalysisError(Exception):
    """Base class for every recoverable problem raised by this library."""


class SchemaError(RelAnalysisError):
    """The relational schema (columns / sub-table layouts) is malformed."""


class DependencyError(RelAnalysisError):
    """A functional or multivalued dependency is malformed."""


class DataError(RelAnalysisError):
    """A concrete relation instance is malformed or violates a dependency."""


class EvidenceError(RelAnalysisError):
    """An evidence document failed independent validation."""
