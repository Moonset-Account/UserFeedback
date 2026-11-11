"""Typed errors used across the package."""


class LoaderError(ValueError):
    """The rule table or data-flow graph document is malformed."""


class AnalysisError(RuntimeError):
    """The fixpoint engine failed to converge or reached an internal limit."""
