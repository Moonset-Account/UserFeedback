"""Typed errors for the connection planner.

All errors carry a stable machine-readable ``code`` so verification harnesses
and audit logs can distinguish rejection reasons without parsing prose.
"""

from __future__ import annotations


class PlanError(Exception):
    """Base class for every planner error."""

    code = "plan_error"

    def __init__(self, message: str, *, host: str | None = None) -> None:
        super().__init__(message)
        self.host = host


class InvalidUrlError(PlanError):
    """The supplied URL is not a supported absolute http(s) URL."""

    code = "invalid_url"


class DomainRuleError(PlanError):
    """A hostname on the CNAME chain does not match the allow list."""

    code = "domain_not_allowed"

    def __init__(self, message: str, *, host: str, offender: str) -> None:
        super().__init__(message, host=host)
        self.offender = offender


class InvalidRuleError(PlanError):
    """A configured domain or network rule is itself malformed."""

    code = "invalid_rule"


class ForbiddenAddressError(PlanError):
    """At least one resolved address falls inside a forbidden network."""

    code = "forbidden_address"

    def __init__(
        self,
        message: str,
        *,
        host: str,
        offending: list[str],
    ) -> None:
        super().__init__(message, host=host)
        self.offending = list(offending)


class ResolverError(PlanError):
    """The resolver adapter failed or returned malformed data."""

    code = "resolver_error"


class UnresolvedError(PlanError):
    """The resolver returned no usable address for the host."""

    code = "no_address"


class PlanExpiredError(PlanError):
    """The resolution snapshot of the plan has passed its expiry."""

    code = "plan_expired"
