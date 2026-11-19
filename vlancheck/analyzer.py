"""Forwarding simulation and boundary analysis."""

from __future__ import annotations

from collections import deque
from typing import Dict, Optional, Set, Tuple

from .model import (
    AnalysisReport,
    BoundaryEvent,
    DropEvent,
    DomainRef,
    PortRef,
    ReachabilityResult,
)
from .topology import Topology


def _refine_untagged_status(base_status: str) -> str:
    """Refine a boundary status for a frame that arrived without a tag."""
    if base_status == "ok":
        return "untagged_ok"
    if base_status == "leak":
        return "untagged_native_misreceive"
    # "unmapped" stays unmapped; BoundaryEvent.sent_tag is None and keeps the
    # information that the tag was stripped.
    return base_status


def analyze(topology: Topology, roots: Optional[list] = None) -> AnalysisReport:
    """Compute reachability for every ingress isolation domain.

    For each root domain ``(device, vlan)`` the simulation injects a frame at
    every port carrying that domain and floods it out every permitted egress
    link *except the cable the frame arrived on* (split horizon).  Forwarding
    state is the triple ``(port, local vlan, arrival link)``: a frame that
    re-enters a port over the same cable hits a visited state, so every
    physical ring terminates, while parallel cables are still traversed
    independently.

    On each crossing the wire tag is either accepted (and translated back to
    the peer's local domain via :attr:`PortConfig.ingress_tag_map`) or, when
    the peer does not accept that label, recorded as a :class:`DropEvent`
    without ever being interpreted as a domain.  Untagged frames enter the
    peer's native/PVID domain.

    Parameters
    ----------
    topology:
        A validated :class:`Topology`.
    roots:
        Optional explicit list of ``(device, vlan)`` roots.  Defaults to
        every domain carried by any port.
    """
    if roots is None:
        root_list = topology.all_domains
    else:
        root_list = []
        for root in roots:
            domain = (root[0], int(root[1]))
            if domain not in topology.all_domains:
                raise KeyError(f"root domain {domain} does not exist in topology")
            root_list.append(domain)

    results: Dict[DomainRef, ReachabilityResult] = {}
    all_drops = []

    for root in root_list:
        result = ReachabilityResult(root=root, reached_domains={root})
        # The forwarding state is the (port, local vlan, link the frame
        # arrived on) triple.  Parallel cables can deliver the same
        # (port, vlan) frame through different links and each must be
        # flooded out the *other* links independently; including the link
        # keeps those cases separate while still making every physical ring
        # terminate, since a frame that re-enters a port over the same cable
        # hits a state it has already visited.
        visited: Set[Tuple[PortRef, int, Optional[int]]] = set()
        # (arrival port, local vlan at that port, id of the link the frame
        # arrived on (None for injected frames), hop count)
        queue: deque = deque(
            (port_ref, root[1], None, 0)
            for port_ref in topology.member_ports(root)
        )

        while queue:
            arrival_port, domain_vlan, arrival_link_id, hops = queue.popleft()
            state = (arrival_port, domain_vlan, arrival_link_id)
            if state in visited:
                continue
            visited.add(state)

            source_domain: DomainRef = (arrival_port[0], domain_vlan)
            egress_port_cfg = topology.ports[arrival_port]
            action = egress_port_cfg.egress_for(domain_vlan)
            if action is None:
                # Frame was accepted here but this port is not an egress
                # member of that domain: nowhere to flood.
                continue

            sent_tag: Optional[int] = action[1] if action[0] == "tag" else None

            # Flood out every link except the one the frame arrived on.
            # This is the split-horizon rule: a frame never re-exits the
            # cable it came in through, on either side of that cable.
            for link_id, peer_ref in topology.neighbors.get(arrival_port, ()):
                if link_id == arrival_link_id:
                    continue
                peer_cfg = topology.ports[peer_ref]
                ingress_map = peer_cfg.ingress_tag_map

                if sent_tag is not None and sent_tag not in ingress_map:
                    # Rejected label: dropped, recorded, never interpreted.
                    all_drops.append(
                        DropEvent(
                            root=root,
                            hops=hops + 1,
                            egress_port=arrival_port,
                            ingress_port=peer_ref,
                            rejected_tag=sent_tag,
                        )
                    )
                    continue

                if sent_tag is None:
                    # Stripped frame falls into the peer's native (PVID)
                    # domain, which may or may not be the intended one.
                    received_vlan = peer_cfg.native_vlan
                else:
                    # Translate the wire tag back into the peer's local
                    # isolation domain (tag id need not equal local vlan).
                    received_vlan = ingress_map[sent_tag]
                received_domain: DomainRef = (peer_ref[0], received_vlan)

                base_status, expected = topology.classify_boundary(
                    source_domain, received_domain
                )
                status = (
                    _refine_untagged_status(base_status)
                    if sent_tag is None
                    else base_status
                )

                event = BoundaryEvent(
                    root=root,
                    hops=hops + 1,
                    egress_port=arrival_port,
                    ingress_port=peer_ref,
                    sent_tag=sent_tag,
                    received_domain=received_domain,
                    status=status,
                    expected_domain=expected,
                )
                result.boundary_events.append(event)
                result.reached_domains.add(received_domain)
                if event.is_leak and result.first_leak is None:
                    result.first_leak = event

                next_state = (peer_ref, received_vlan, link_id)
                if next_state not in visited:
                    queue.append((peer_ref, received_vlan, link_id, hops + 1))

        results[root] = result

    return AnalysisReport(results=results, drops=all_drops)
