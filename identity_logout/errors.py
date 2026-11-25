"""Errors raised by the logout-completion evidence decider."""


class LogoutDecisionError(ValueError):
    """Base class for invalid decider input."""


class InvalidEventError(LogoutDecisionError):
    """An event or watermark cannot be normalized."""


class EventConflictError(LogoutDecisionError):
    """Two pieces of evidence claim incompatible immutable facts."""


class BarrierConfigurationError(LogoutDecisionError):
    """A revocation barrier cannot be interpreted."""
