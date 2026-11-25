"""Simplified LACP aggregation controller.

The controller consumes four kinds of events:

* port up / down (link layer);
* neighbor LACP advertisements carrying the peer system identifier, the
  aggregation key and the peer's view of bidirectional synchronization;
* advertisement timeouts (evaluated from an adjustable clock);
* marker responses proving that previously sent frames have been drained.

Selection rules (see README for the full model):

1. A port is *syncable* when its link is up, a fresh advertisement is held
   and both sides report synchronization.
2. Syncable ports are grouped by ``(peer_system_id, aggregation_key)``.
3. The group with the most members wins; on equal size the incumbent group
   is kept, otherwise groups are ordered deterministically by identifier.

Only synchronized members of the selected group may *collect* (receive).
Distribution (sending) additionally requires that every outstanding frame
of every stopped member has been proven drained by a marker response of the
generation in which the drain was initiated, or that the member's link has
gone down.  An advertisement expiry only removes synchronization; it is
never accepted as drain proof, and a marker response tagged with an older
generation is recorded as late but releases nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from .clock import ManualClock

# A group is identified by the peer system number and the aggregation key.
GroupId = Tuple[Any, Any]
# Marker used for drains whose port no longer carries an advertisement; the
# second tuple element is the port id so entries stay distinct.
DETACHED_GROUP = "__detached__"


@dataclass
class NeighborInfo:
    """Last neighbor advertisement recorded for a port."""

    system_id: Any
    key: Any
    synced: bool
    deadline: float

    def is_fresh(self, now: float) -> bool:
        return now < self.deadline


@dataclass
class MarkerRequest:
    """A marker request emitted when a member stops distributing while
    frames of generation ``generation`` are still outstanding."""

    port_id: Any
    generation: int
    sent_at: float


@dataclass
class PortState:
    """Public snapshot of a single port's state."""

    port_id: Any
    up: bool
    neighbor: Optional[NeighborInfo]
    in_selected_group: bool
    collecting: bool
    distributing: bool
    outstanding_frames: int


@dataclass
class _Port:
    """Mutable internal port record."""

    port_id: Any
    up: bool = False
    neighbor: Optional[NeighborInfo] = None
    in_selected_group: bool = False
    distributing: bool = False
    outstanding: int = 0
    # Generation for which a marker response is currently awaited.
    pending_drain_gen: Optional[int] = None


class LacpController:
    """Event-driven LACP aggregator driven by a :class:`ManualClock`."""

    def __init__(self, ttl: float = 30.0, clock: Optional[ManualClock] = None):
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        self._ttl = float(ttl)
        self.clock = clock if clock is not None else ManualClock()
        self._ports: Dict[Any, _Port] = {}
        self._selected_id: Optional[GroupId] = None
        self._selected: FrozenSet[Any] = frozenset()
        self._generation: int = 0
        # Ports whose outstanding frames still await drain proof.
        self._pending_ports: set = set()
        self.marker_requests: List[MarkerRequest] = []
        self.stale_responses: List[Tuple[Any, int]] = []
        self.ignored_events: List[Tuple[str, Any]] = []

    # ------------------------------------------------------------------ #
    # Event handling
    # ------------------------------------------------------------------ #
    def port_up(self, port_id: Any) -> None:
        """Bring a physical port's link up (registering it on first use)."""
        port = self._ports.get(port_id)
        if port is None:
            port = _Port(port_id=port_id)
            self._ports[port_id] = port
        port.up = True
        self._reconcile()

    def port_down(self, port_id: Any) -> None:
        """Take a physical port's link down.

        A down link removes the port from the selected group and collection.
        It is also accepted as drain proof: frames can never traverse that
        member again, so its pending drain is released at once.
        """
        port = self._ports.get(port_id)
        if port is None:
            self.ignored_events.append(("port_down_unknown", port_id))
            return
        port.up = False
        port.neighbor = None
        port.in_selected_group = False
        port.distributing = False
        port.outstanding = 0
        port.pending_drain_gen = None
        self._pending_ports.discard(port_id)
        self._reconcile()

    def receive_advertisement(
        self,
        port_id: Any,
        system_id: Any,
        key: Any,
        synced: bool,
        ttl: Optional[float] = None,
    ) -> None:
        """Record a neighbor LACP advertisement on ``port_id``.

        Advertisements arriving while the link is down (or on an unknown
        port) are ignored.  A fresh advertisement replaces the previous one,
        even when the system identifier or aggregation key changed; the
        grouping logic then migrates the port on the next reconciliation.
        """
        port = self._ports.get(port_id)
        if port is None or not port.up:
            self.ignored_events.append(("advertisement_down_or_unknown", port_id))
            return
        lifetime = self._ttl if ttl is None else ttl
        if lifetime <= 0:
            raise ValueError("advertisement ttl must be positive")
        port.neighbor = NeighborInfo(
            system_id=system_id,
            key=key,
            synced=bool(synced),
            deadline=self.clock.now() + float(lifetime),
        )
        self._reconcile()

    def receive_marker_response(self, port_id: Any, generation: int) -> None:
        """Process a marker response for a drain initiated in ``generation``.

        Only the exact generation for which the port is currently pending
        releases the drain.  A response belonging to an older generation is
        a *late* response: it is recorded in ``stale_responses`` but never
        releases anything, because it cannot prove the newer frames were
        drained.
        """
        port = self._ports.get(port_id)
        if (
            port is None
            or port_id not in self._pending_ports
            or port.pending_drain_gen != generation
        ):
            self.stale_responses.append((port_id, generation))
            return
        port.pending_drain_gen = None
        port.outstanding = 0
        self._pending_ports.discard(port_id)
        self._reconcile()

    def tick(self, seconds: float) -> None:
        """Advance the virtual clock and process any timeouts."""
        self.clock.advance(seconds)
        self._reconcile()

    # ------------------------------------------------------------------ #
    # Data plane
    # ------------------------------------------------------------------ #
    def can_collect(self, port_id: Any) -> bool:
        """Return whether the port may currently receive frames.

        Collection requires: link up, membership of the selected group and a
        fresh bidirectionally synchronized advertisement.
        """
        port = self._ports.get(port_id)
        if port is None or not port.up or not port.in_selected_group:
            return False
        return self._is_synced(port)

    def can_distribute(self, port_id: Any) -> bool:
        """Return whether the port may currently send frames."""
        port = self._ports.get(port_id)
        return port is not None and port.distributing

    def send_frame(self, port_id: Any) -> bool:
        """Attempt to send one frame through ``port_id``.

        Returns ``True`` when the frame was accepted and counted as
        outstanding, ``False`` when the port is not allowed to distribute.
        """
        port = self._ports.get(port_id)
        if port is None or not port.distributing:
            return False
        port.outstanding += 1
        return True

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    @property
    def generation(self) -> int:
        return self._generation

    @property
    def selected_group(self) -> Optional[GroupId]:
        return self._selected_id

    def collecting_ports(self) -> FrozenSet[Any]:
        return frozenset(pid for pid in self._ports if self.can_collect(pid))

    def distributing_ports(self) -> FrozenSet[Any]:
        return frozenset(
            pid for pid, port in self._ports.items() if port.distributing
        )

    def pending_drains(self) -> Dict[GroupId, FrozenSet[Any]]:
        """Return pending drain ports grouped by their last known peer."""
        out: Dict[GroupId, set] = {}
        for pid in self._pending_ports:
            port = self._ports[pid]
            if port.neighbor is not None:
                gid: GroupId = (port.neighbor.system_id, port.neighbor.key)
            else:
                gid = (DETACHED_GROUP, pid)
            out.setdefault(gid, set()).add(pid)
        return {gid: frozenset(members) for gid, members in out.items()}

    def port_state(self, port_id: Any) -> PortState:
        port = self._ports[port_id]
        return PortState(
            port_id=port.port_id,
            up=port.up,
            neighbor=port.neighbor,
            in_selected_group=port.in_selected_group,
            collecting=self.can_collect(port_id),
            distributing=port.distributing,
            outstanding_frames=port.outstanding,
        )

    def states(self) -> Dict[Any, PortState]:
        return {pid: self.port_state(pid) for pid in self._ports}

    # ------------------------------------------------------------------ #
    # Core reconciliation
    # ------------------------------------------------------------------ #
    def _is_synced(self, port: _Port) -> bool:
        return (
            port.up
            and port.neighbor is not None
            and port.neighbor.synced
            and port.neighbor.is_fresh(self.clock.now())
        )

    def _syncable_groups(self) -> Dict[GroupId, set]:
        groups: Dict[GroupId, set] = {}
        for port in self._ports.values():
            if self._is_synced(port) and port.neighbor is not None:
                gid = (port.neighbor.system_id, port.neighbor.key)
                groups.setdefault(gid, set()).add(port.port_id)
        return groups

    def _advertised_group(self, port: _Port) -> Optional[GroupId]:
        """Group a port advertises membership of, regardless of freshness.

        The advertisement's system/key still identifies which logical group
        the port belongs to even after it goes stale: losing synchronization
        pauses collection/distribution but does not, by itself, remove the
        port from the group.
        """
        if not port.up or port.neighbor is None:
            return None
        return port.neighbor.system_id, port.neighbor.key

    @staticmethod
    def _group_order(gid: GroupId) -> Tuple[str, str]:
        # Stringify both elements so ordering stays total and deterministic
        # even with mixed identifier types (e.g. ints alongside strings).
        return str(gid[0]), str(gid[1])

    def _choose(
        self, groups: Dict[GroupId, set]
    ) -> Tuple[Optional[GroupId], FrozenSet[Any]]:
        """Largest group; incumbent kept on ties; else lowest identifier.

        Groups are visited in identifier order, so when no incumbent exists
        the first group seen at a given size wins the tie deterministically.
        """
        chosen: Optional[GroupId] = None
        chosen_members: FrozenSet[Any] = frozenset()
        for gid in sorted(groups, key=self._group_order):
            members = frozenset(groups[gid])
            if chosen is None or len(members) > len(chosen_members):
                chosen, chosen_members = gid, members
            elif len(members) == len(chosen_members) and gid == self._selected_id:
                # On equal size only the incumbent may replace the current
                # pick; otherwise the earlier identifier (first in sorted
                # order) keeps the slot.
                chosen, chosen_members = gid, members
        return chosen, chosen_members

    def _begin_drain(self, port: _Port) -> None:
        """Start (or re-arm) awaiting drain proof for a port with frames."""
        if port.outstanding == 0:
            return
        if port.pending_drain_gen is None and port.up:
            self.marker_requests.append(
                MarkerRequest(
                    port_id=port.port_id,
                    generation=self._generation,
                    sent_at=self.clock.now(),
                )
            )
        # A member already pending from an earlier generation is re-armed
        # with the current generation: a marker response for the older
        # generation cannot prove the newer outstanding frames were drained.
        port.pending_drain_gen = self._generation
        self._pending_ports.add(port.port_id)

    def _evict(self, port: _Port) -> None:
        """Remove a port from the selected group and stop its distribution."""
        port.in_selected_group = False
        port.distributing = False
        self._begin_drain(port)

    def _reconcile(self) -> None:
        groups = self._syncable_groups()
        chosen, chosen_members = self._choose(groups)

        # Membership of the selected group is determined by what each port
        # advertises, not by advertisement freshness.  A stale advertisement
        # means the port lost synchronization (collection pauses), but the
        # port stays a member until its advertised group changes or its link
        # goes down.
        effective: Dict[Optional[GroupId], set] = {}
        for pid, port in self._ports.items():
            effective.setdefault(self._advertised_group(port), set()).add(pid)
        effective_members = frozenset(effective.get(chosen, set()))

        if chosen != self._selected_id:
            # Every identity change, including the very first selection and
            # collapse to no group, opens a new generation.  Old members stop
            # distributing before any new member is allowed to send, and any
            # member with outstanding frames is (re-)armed for this
            # generation's drain.
            self._generation += 1
            old_members = set(self._selected)
            self._selected_id = chosen
            self._selected = effective_members
            for pid in old_members:
                self._evict(self._ports[pid])
            # Ports already draining from an earlier switch are not members
            # of the old set, but their outstanding frames also cannot be
            # proven by an older-generation marker response: re-arm them.
            for pid in set(self._pending_ports) - old_members:
                self._begin_drain(self._ports[pid])
            for pid in effective_members:
                self._ports[pid].in_selected_group = True
        else:
            # Same identity.  Only a port whose advertised group changed or
            # whose link went down truly leaves the group (and must drain).
            # A merely stale advertisement keeps membership intact.
            leavers = self._selected - effective_members
            for pid in leavers:
                self._evict(self._ports[pid])
            for pid in effective_members - self._selected:
                self._ports[pid].in_selected_group = True
            self._selected = effective_members

        self._rebuild_distribution()

    def _rebuild_distribution(self) -> None:
        for port in self._ports.values():
            allowed = (
                port.in_selected_group
                and self._is_synced(port)
                and not self._pending_ports
            )
            # Membership loss is handled by the explicit eviction paths in
            # ``_reconcile`` (which also arm the drain).  Here a still-selected
            # port that lost synchronization simply pauses distribution until
            # its advertisement is fresh again; no group change occurred.
            if allowed:
                port.distributing = True
            else:
                port.distributing = False
