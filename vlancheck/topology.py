"""Topology container: ports, explicit cabling and prescribed domain pairs."""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from .model import ConfigError, DomainRef, Link, PortConfig, PortRef


def _domain_key(domain: object) -> DomainRef:
    if (
        not isinstance(domain, (tuple, list))
        or len(domain) != 2
        or not isinstance(domain[0], str)
        or not isinstance(domain[1], int)
    ):
        raise ConfigError(f"domain must be [device, vlan_id], got {domain!r}")
    return (domain[0], domain[1])


class Topology:
    """Validated network topology used by the analyzer.

    ``domain_pairs`` declares the only boundaries that are checked against an
    expected peer domain.  Each side of a pair is a local ``(device, vlan)``
    domain; both sides are stored as equivalent keys pointing at the same
    frozenset of paired domains.  Crossings without a pair are reported as
    ``unmapped`` rather than as leaks.
    """

    def __init__(
        self,
        ports: Iterable[PortConfig],
        links: Iterable[Link],
        domain_pairs: Iterable[Iterable[object]] = (),
    ) -> None:
        self.ports: Dict[PortRef, PortConfig] = {}
        self.devices: Set[str] = set()
        for port in ports:
            if not isinstance(port, PortConfig):
                raise ConfigError(f"expected PortConfig, got {port!r}")
            if port.ref in self.ports:
                raise ConfigError(f"duplicate port definition: {port.ref}")
            self.ports[port.ref] = port
            self.devices.add(port.device)

        self.links: List[Link] = []
        # neighbors[port] = ordered list of (link index, peer port)
        self.neighbors: Dict[PortRef, List[Tuple[int, PortRef]]] = defaultdict(list)
        seen_cables: Set[FrozenSet[PortRef]] = set()
        for link_id, link in enumerate(links):
            if not isinstance(link, Link):
                raise ConfigError(f"expected Link, got {link!r}")
            for end in (link.a, link.b):
                if end not in self.ports:
                    raise ConfigError(
                        f"link {link.a} <-> {link.b} references undefined port {end}"
                    )
            cable = frozenset((link.a, link.b))
            if cable in seen_cables:
                raise ConfigError(
                    f"duplicate link between {link.a} and {link.b}"
                )
            seen_cables.add(cable)
            self.links.append(link)
            self.neighbors[link.a].append((link_id, link.b))
            self.neighbors[link.b].append((link_id, link.a))

        # Deterministic neighbor ordering for reproducible "first leak".
        for neighbors in self.neighbors.values():
            neighbors.sort(key=lambda item: item[1])

        # Normalize domain pairs into an equivalence lookup.
        self._pair_membership: Dict[DomainRef, FrozenSet[DomainRef]] = {}
        for pair in domain_pairs or ():
            members = [_domain_key(domain) for domain in pair]
            if len(members) < 2:
                raise ConfigError(f"domain pair needs at least two members: {pair!r}")
            for member in members:
                if member[0] not in self.devices:
                    raise ConfigError(
                        f"domain pair references unknown device {member[0]}"
                    )
                if not self._port_exists_for_domain(member):
                    raise ConfigError(
                        f"domain pair references domain {member} that no port carries"
                    )
            group = frozenset(members)
            for member in members:
                existing = self._pair_membership.get(member)
                if existing is not None and existing != group:
                    raise ConfigError(
                        f"domain {member} declared in conflicting pairs "
                        f"{sorted(existing)} and {sorted(group)}"
                    )
                self._pair_membership[member] = group

    def _port_exists_for_domain(self, domain: DomainRef) -> bool:
        device, vid = domain
        return any(
            port.device == device and vid in port.member_vlans
            for port in self.ports.values()
        )

    @property
    def all_domains(self) -> List[DomainRef]:
        domains: Set[DomainRef] = set()
        for port in self.ports.values():
            for vid in port.member_vlans:
                domains.add((port.device, vid))
        return sorted(domains)

    def member_ports(self, domain: DomainRef) -> List[PortRef]:
        device, vid = domain
        return sorted(
            port.ref
            for port in self.ports.values()
            if port.device == device and vid in port.member_vlans
        )

    def expected_peer_domains(self, domain: DomainRef) -> Optional[FrozenSet[DomainRef]]:
        """Return the prescribed pair group for ``domain`` or ``None``."""
        return self._pair_membership.get(domain)

    def classify_boundary(
        self, source_domain: DomainRef, received_domain: DomainRef
    ) -> Tuple[str, Optional[DomainRef]]:
        """Classify a crossing using only the prescribed domain pairs.

        Returns ``(status, expected_domain)``.

        * ``ok``       - source and receiver are in the same prescribed pair.
        * ``leak``     - the source has a prescribed pair but the receiver is
                         not one of its paired domains (frames were
                         interpreted as another isolation domain).
        * ``unmapped`` - the source domain is not covered by any pair, so
                         this boundary is outside the checked scope.

        ``expected_domain`` is the foreign member of the source's pair when
        one exists, otherwise ``None``.
        """
        source_group = self._pair_membership.get(source_domain)
        if source_group is None:
            return "unmapped", None

        foreign = frozenset(source_group - {source_domain})
        expected = sorted(foreign)[0] if foreign else None

        if received_domain in source_group:
            return "ok", expected
        return "leak", expected
