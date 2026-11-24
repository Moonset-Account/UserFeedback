"""Errors raised by the block-cache planning and verification library."""


class BlockCacheError(Exception):
    """Base class for all library errors."""


class InvalidInputError(BlockCacheError, ValueError):
    """The caller supplied an invalid page, request, or checkpoint."""


class InvalidRequestError(BlockCacheError):
    """A request sequence violates pin/reference semantics."""


class InfeasiblePlanError(BlockCacheError):
    """No legal cache schedule can execute the workload."""


class PlanVerificationError(BlockCacheError):
    """A plan does not satisfy the tick-by-tick cache invariants."""
