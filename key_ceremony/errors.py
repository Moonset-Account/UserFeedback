class CeremonyInputError(ValueError):
    """Raised when a ceremony specification is structurally invalid."""


class SolverLimitError(RuntimeError):
    """Raised only by optional guarded solver modes.

    The default solver does not impose an assignment-count cap.  This error is
    useful for callers that prefer an explicit failure over a long exhaustive
    search.
    """
