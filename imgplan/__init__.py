"""imgplan - image proxy connection planning.

Turn an *allowed* target URL into an explicit, auditable *connection plan*:

* the request host (and every name on its full CNAME chain) must match the
  configured domain allow rules (label based);
* every address returned by the resolver is checked against the forbidden IP
  networks, including IPv4-mapped IPv6 addresses which are judged by the
  embedded IPv4 address;
* the resulting plan pins one selected IP, the port, the Host/SNI names, the
  full approved address set and the resolution snapshot expiry;
* a connection adapter built around the plan is physically unable to dial an
  address that is not part of the approved set;
* redirects require a brand new plan per hop - previous resolution results and
  authorizations are never carried over;
* an expired plan cannot be connected to; the target must be re-planned.

The package performs no real network I/O on its own: name resolution is
injected as a resolver adapter and the clock is injected as a clock adapter.
"""

from .errors import (
    DomainRuleError,
    ForbiddenAddressError,
    InvalidRuleError,
    InvalidUrlError,
    PlanError,
    PlanExpiredError,
    ResolverError,
    UnresolvedError,
)
from .domains import DomainAllowList, DomainRule, normalize_hostname
from .netpolicy import ForbiddenNetworks, effective_ip, is_ipv4_mapped
from .resolver import InMemoryResolver, Resolution, ResolverAdapter
from .clock import Clock, FakeClock, SystemClock
from .planner import ConnectionPlan, ConnectionPlanner
from .connector import PlannedConnection, PlannedConnector
from .session import PlannedSession, HopResult

__all__ = [
    "DomainRuleError",
    "ForbiddenAddressError",
    "InvalidRuleError",
    "InvalidUrlError",
    "PlanError",
    "PlanExpiredError",
    "ResolverError",
    "UnresolvedError",
    "DomainAllowList",
    "DomainRule",
    "normalize_hostname",
    "ForbiddenNetworks",
    "effective_ip",
    "is_ipv4_mapped",
    "InMemoryResolver",
    "Resolution",
    "ResolverAdapter",
    "Clock",
    "FakeClock",
    "SystemClock",
    "ConnectionPlan",
    "ConnectionPlanner",
    "PlannedConnection",
    "PlannedConnector",
    "PlannedSession",
    "HopResult",
]

__version__ = "1.0.0"
