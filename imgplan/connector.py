"""Connection adapter that is physically pinned to one connection plan.

The connector accepts a frozen :class:`ConnectionPlan` at construction time
and will only establish connections to addresses that belong to the plan's
approved set. Re-querying the name immediately before dialing cannot widen
that set: any address not present in the plan is refused, and an expired
plan refuses every connection until the caller re-plans.
"""

from __future__ import annotations

from dataclasses import dataclass

from .clock import Clock
from .errors import PlanExpiredError, PlanError
from .netpolicy import effective_ip
from .planner import ConnectionPlan
from .resolver import ResolverAdapter


@dataclass(frozen=True)
class PlannedConnection:
    """Record of a (simulated) connection attempt."""

    plan_id: str
    remote_ip: str
    port: int
    host_header: str
    server_name: str
    established_at: float


class PlannedConnector:
    """The only connection adapter callers may use for a planned target."""

    def __init__(self, plan: ConnectionPlan, clock: Clock) -> None:
        self._plan = plan
        self._clock = clock
        self._connections: list[PlannedConnection] = []

    @property
    def plan(self) -> ConnectionPlan:
        return self._plan

    @property
    def connections(self) -> tuple[PlannedConnection, ...]:
        return tuple(self._connections)

    def _approved_set(self) -> set:
        return {effective_ip(ip) for ip in self._plan.approved_addresses}

    def connect(
        self,
        address: str | None = None,
        *,
        resolver: ResolverAdapter | None = None,
    ) -> PlannedConnection:
        """Open a connection to *address*.

        * ``address=None`` dials the plan's deterministic ``selected_ip``.
        * An explicit *address* is only accepted when it is a member of the
          plan's approved address set - anything else, including an address
          obtained by re-resolving the hostname right now, is refused.
        * Passing *resolver* simulates a "re-check DNS first" client behavior:
          the freshly resolved addresses are checked **against the plan**, not
          the other way round. A rebinding answer that dropped the approved IP
          therefore cannot replace it.
        """
        plan = self._plan
        if plan.is_expired(self._clock):
            raise PlanExpiredError(
                f"plan {plan.plan_id} expired at {plan.expires_at}; re-plan required",
                host=plan.request_host,
            )

        if resolver is not None:
            fresh = resolver.resolve(plan.request_host)
            fresh_ips = {effective_ip(ip) for ip in fresh.addresses}
            approved = self._approved_set()
            # The fresh answer may only narrow the choice; it can never add
            # addresses that the plan did not authorize.
            unauthorized = fresh_ips - approved
            if unauthorized:
                raise PlanError(
                    "re-resolution returned addresses not present in the approved "
                    f"plan: {sorted(str(ip) for ip in unauthorized)}",
                    host=plan.request_host,
                )
            candidate_pool = fresh_ips & approved
            if not candidate_pool:
                raise PlanError(
                    "re-resolution no longer contains any approved address; "
                    "the plan must be re-authorized before connecting",
                    host=plan.request_host,
                )

        if address is None:
            target = effective_ip(plan.selected_ip)
        else:
            try:
                target = effective_ip(address)
            except ValueError as exc:
                raise PlanError(f"malformed dial address {address!r}") from exc

        if target not in self._approved_set():
            raise PlanError(
                f"refusing to dial {target}: not an address approved by plan {plan.plan_id}",
                host=plan.request_host,
            )

        record = PlannedConnection(
            plan_id=plan.plan_id,
            remote_ip=str(target),
            port=plan.port,
            host_header=plan.host_header,
            server_name=plan.server_name,
            established_at=self._clock.now(),
        )
        self._connections.append(record)
        return record
