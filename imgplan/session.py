"""Redirect-aware session: every hop is planned and authorized independently.

Nothing from a previous hop is carried into the next one: no resolved
addresses, no approved plan, no connector. Each hop calls the planner again
with its own URL, so a name that was allowed on hop 1 can be (and in the
rebinding demonstration, is) rejected on hop 2.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .clock import Clock
from .connector import PlannedConnection, PlannedConnector
from .planner import ConnectionPlan, ConnectionPlanner


@dataclass(frozen=True)
class HopResult:
    """Outcome of one hop: either an authorized plan or a rejection."""

    hop: int
    url: str
    allowed: bool
    plan: ConnectionPlan | None
    connection: PlannedConnection | None
    rejection_code: str | None
    rejection_message: str | None


@dataclass
class PlannedSession:
    """Drives hop-by-hop planning through one :class:`ConnectionPlanner`."""

    planner: ConnectionPlanner
    clock: Clock
    _hops: list[HopResult] = field(default_factory=list)

    @property
    def hops(self) -> tuple[HopResult, ...]:
        return tuple(self._hops)

    def follow(self, url: str) -> HopResult:
        """Plan *url* as a fresh hop and, if allowed, open one connection."""
        hop_number = len(self._hops) + 1
        try:
            # Deliberately a brand new plan: no state is inherited.
            plan = self.planner.plan_url(url)
        except Exception as exc:  # noqa: BLE001 - every PlanError carries a code
            result = HopResult(
                hop=hop_number,
                url=url,
                allowed=False,
                plan=None,
                connection=None,
                rejection_code=getattr(exc, "code", "plan_error"),
                rejection_message=str(exc),
            )
            self._hops.append(result)
            return result

        connector = PlannedConnector(plan, self.clock)
        connection = connector.connect()
        result = HopResult(
            hop=hop_number,
            url=url,
            allowed=True,
            plan=plan,
            connection=connection,
            rejection_code=None,
            rejection_message=None,
        )
        self._hops.append(result)
        return result
